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
let cvdSource = "tick";
let klineLtf = 10;    // 切回 K线口径时保留上次选择，默认同 Pine 的 10S。
let ltf = 0;         // 协议：0=tick，正数=实际小周期 K线。
let view = "candle";  // 主图视图: candle=K线, footprint=足迹图
let fpBars = [];      // 足迹 bar: {time, levels: [[price, buy, sell], ...按价格升序]}
let fpBarSpacing = null;   // 进足迹模式前的 barSpacing, 退出时恢复
let barRevision = -1, fpRevision = -1;
let watchdog = null;
let symbol = new URLSearchParams(location.search).get("symbol") || "KQ.m@SHFE.fu";
let ws = null;
let wsGeneration = 0;
let reconnectTimer = null;
let loadGeneration = 0;

$("symbol").value = symbol;

// ---------- 足迹图自定义 series (lightweight-charts v5 custom series) ----------
// 数据项: {time, levels: [[price, buy, sell], ...按价格升序]}
// 每档一格分左右两半(左卖右买), 暖色热力底色按档量强度渐变;
// K线轮廓(影线+柱体框)垫在格子下; POC(最大量档)白框, 对角不平衡(>=3:1)色框

const FP = {
  imbRatio: 3,                              // 对角不平衡阈值: 买[j] vs 卖[j-1] 或 卖[j] vs 买[j+1]
  imbMinVol: 10,                            // 优势侧最小量, 防小数字噪声
  imbBuyColor: "#00e676", imbSellColor: "#f23645",
  upColor: "#f0b90d", downColor: "#ef6c00", // K线轮廓: 涨金 跌橙
  pocColor: "rgba(232, 234, 237, 0.9)",
};

class FootprintRenderer {
  constructor() {
    this._data = null;
    this._options = null;
  }
  update(data, options) {
    this._data = data;
    this._options = options;
  }
  _heat(ratio) {   // 暖色热力: 同一色系, 亮度 = 档量 / bar 最大档量
    return `rgba(245, 166, 35, ${0.08 + 0.87 * ratio})`;
  }
  draw(target, priceConverter) {
    if (!this._data || !this._data.bars.length) return;
    const { bars, barSpacing } = this._data;
    const tickSize = this._options?.tickSize;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const cellW = Math.max(barSpacing * 0.85, 6);
      const halfW = cellW / 2;
      const showText = cellW >= 42;
      ctx.font = "9px Consolas, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (let i = 0; i < bars.length; i++) {
        const d = bars[i].originalData;
        const levels = d.levels;
        if (!levels || !levels.length) continue;
        const x = bars[i].x - cellW / 2;
        const ys = levels.map((lv) => priceConverter(lv[0]));
        const diagonals = FlowData.diagonalVolumes(levels, tickSize);
        // 稀疏成交档位之间可能隔了多个 tick，行高只按真实最小变动价位计算。
        let rowH = 6;
        if (tickSize > 0 && ys[0] != null) {
          const y2 = priceConverter(levels[0][0] + tickSize);
          if (y2 != null && y2 !== ys[0]) rowH = Math.abs(y2 - ys[0]);
        }
        // K线轮廓: 影线(最高-最低) + 柱体框(开-收), 垫在格子下
        if (d.high != null && d.low != null) {
          const yH = priceConverter(d.high), yL = priceConverter(d.low);
          if (yH != null && yL != null) {
            const frameColor = d.close >= d.open ? FP.upColor : FP.downColor;
            ctx.strokeStyle = frameColor;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(bars[i].x, yH);
            ctx.lineTo(bars[i].x, yL);
            ctx.stroke();
            const yO = priceConverter(d.open), yC = priceConverter(d.close);
            if (yO != null && yC != null) {
              ctx.lineWidth = 1.5;
              ctx.strokeRect(x, Math.min(yO, yC), cellW, Math.max(Math.abs(yC - yO), 1));
            }
          }
        }
        let maxVol = 0, pocIdx = -1;
        for (let j = 0; j < levels.length; j++) {
          const v = levels[j][1] + levels[j][2];
          if (v > maxVol) { maxVol = v; pocIdx = j; }
        }
        if (maxVol <= 0) continue;
        for (let j = 0; j < levels.length; j++) {
          const y = ys[j];
          if (y == null) continue;
          const buy = levels[j][1], sell = levels[j][2];
          const top = y - rowH / 2;
          const h = Math.max(rowH - 1, 1);
          ctx.fillStyle = this._heat(sell / maxVol);
          ctx.fillRect(x, top, halfW, h);
          ctx.fillStyle = this._heat(buy / maxVol);
          ctx.fillRect(x + halfW, top, cellW - halfW, h);
          if (showText && rowH >= 9) {
            ctx.fillStyle = "rgba(19, 23, 34, 0.6)";      // 左右半格分隔线
            ctx.fillRect(x + halfW, top, 1, h);
            ctx.fillStyle = "rgba(232, 234, 237, 0.92)";
            ctx.fillText(String(sell), x + halfW / 2, y);
            ctx.fillText(String(buy), x + halfW * 1.5, y);
            // 对角不平衡: 买[j] >= 3*卖[j-1] 或 卖[j] >= 3*买[j+1] (levels 按价格升序)
            const diagonal = diagonals[j];
            let imbColor = null;
            if (diagonal && d.coverage === "complete") {
              if (buy >= FP.imbRatio * Math.max(diagonal.sellBelow, 1) && buy >= FP.imbMinVol) imbColor = FP.imbBuyColor;
              else if (sell >= FP.imbRatio * Math.max(diagonal.buyAbove, 1) && sell >= FP.imbMinVol) imbColor = FP.imbSellColor;
            }
            if (imbColor) {
              ctx.strokeStyle = imbColor;
              ctx.lineWidth = 1.5;
              ctx.strokeRect(x + 0.5, top + 0.5, cellW - 1, h - 1);
            }
            if (j === pocIdx) {                            // POC 白框画内侧, 与不平衡框共存时两层都可见
              ctx.strokeStyle = FP.pocColor;
              ctx.lineWidth = 1.5;
              ctx.strokeRect(x + 2.5, top + 2.5, Math.max(cellW - 5, 1), Math.max(h - 5, 1));
            }
          }
        }
      }
    });
  }
}

class FootprintSeries {
  constructor() {
    this._renderer = new FootprintRenderer();
  }
  defaultOptions() { return { priceLineVisible: false, lastValueVisible: false }; }
  renderer() { return this._renderer; }
  update(data, options) { this._renderer.update(data, options); }
  priceValueBuilder(plotRow) {
    const levels = plotRow.levels;
    return [Math.min(levels[0][0], plotRow.low ?? Infinity),
            Math.max(levels[levels.length - 1][0], plotRow.high ?? -Infinity),
            plotRow.close ?? levels[levels.length - 1][0]];
  }
  isWhitespace(data) { return !data.levels || !data.levels.length; }
  destroy() {}
}

// ---------- 图表初始化 ----------

const chart = LightweightCharts.createChart($("chart"), {
  autoSize: true,
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

// 足迹图 series (pane 0, 默认隐藏; 视图切到足迹图时显示, tickSize 由 /api/footprint 下发)
const fpSeries = chart.addCustomSeries(new FootprintSeries(), { visible: false, tickSize: 1 }, 0);

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
    const rel = { vol: d.rvol, posd: d.rpos, negd: d.rneg, buy: d.rbuy, sell: d.rsell }[kind][i];
    return FlowData.relativeLevel(kind, rel, m);
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
  const cumulative = mode === "crvol" ? derived.crv : mode === "cvd" ? bars.map((b) => b.cvd) : null;
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
      const arr = cumulative;
      const shape = mode === "cvd" ? FlowData.cvdCandle(b) :
        (arr[i] == null || i === 0 || arr[i - 1] == null ? null :
          {time: b.time, open: arr[i - 1], high: Math.max(arr[i - 1], arr[i]),
           low: Math.min(arr[i - 1], arr[i]), close: arr[i]});
      if (!shape) continue;
      const lvl = mode === "crvol" ? levelOf("vol", i)
                                  : levelOf(b.delta > 0 ? "posd" : "negd", i);
      // 原指标: CRVOL 蜡烛按 K线阴阳着色, CVD 蜡烛按 delta 正负着色
      const col = mode === "crvol" ? colorFor(up, lvl) : colorFor(b.delta > 0, lvl);
      candles.push({ ...shape, color: col, wickColor: col });
    }
  }
  return { hist, histSell, candles };
}

function renderSuite() {
  if (!derived) return;
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
  const latestHist = hist[hist.length - 1];
  histA.update(latestHist?.time === b.time ? latestHist : { time: b.time });
  if (mode === "bsv") {
    const latestSell = histSell[histSell.length - 1];
    histB.update(latestSell?.time === b.time ? latestSell : { time: b.time });
  }
  if (mode === "crvol" || mode === "cvd") {
    const latestCandle = candles[candles.length - 1];
    candleSuite.update(latestCandle?.time === b.time ? latestCandle : { time: b.time });
  }
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
  const quality = view === "footprint" ? fpBars.find((fp) => fp.time === b.time)?.coverage : b.coverage;
  $("coverage").textContent = "覆盖:" + (({complete: "完整", partial: "部分", missing: "缺失", legacy: "旧历史"})[quality] || "缺失");
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
function updateSplitSelection() {
  ltf = FlowData.splitLtf(cvdSource, klineLtf);
  $("ltf").disabled = cvdSource === "tick";
  $("ltf").value = String(klineLtf);
  loadHistory().catch((error) => { setStatus(false, "加载失败: " + error.message); });
}
$("cvd-source").addEventListener("change", (e) => {
  cvdSource = e.target.value;
  updateSplitSelection();
});
$("ltf").addEventListener("change", (e) => {
  klineLtf = parseInt(e.target.value, 10);
  updateSplitSelection();
});
$("apply").addEventListener("click", () => {
  const s = $("symbol").value.trim();
  if (s) location.search = "?symbol=" + encodeURIComponent(s);
});

// ---------- 足迹图视图切换与数据 ----------

function ohlcOf(t) {   // bars 按 time 升序, 二分查找出 footprint bar 对应的 K线 OHLC
  let lo = 0, hi = bars.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].time === t) return bars[mid];
    if (bars[mid].time < t) lo = mid + 1; else hi = mid - 1;
  }
  return null;
}

const toFpItem = (b) => {
  const item = { time: b.time, levels: b.levels, coverage: b.coverage };
  const k = ohlcOf(b.time);
  if (k) { item.open = k.open; item.high = k.high; item.low = k.low; item.close = k.close; }
  return item;
};

$("view").addEventListener("change", (e) => setView(e.target.value));

function setView(v) {
  view = v;
  const isFp = v === "footprint";
  candleSeries.applyOptions({ visible: !isFp });
  emaSeries.forEach((s) => s.applyOptions({ visible: !isFp }));
  fpSeries.applyOptions({ visible: isFp });
  if (isFp) {
    fpBarSpacing = chart.timeScale().options().barSpacing;
    chart.timeScale().applyOptions({ barSpacing: 60 });
  } else if (fpBarSpacing != null) {
    chart.timeScale().applyOptions({ barSpacing: fpBarSpacing });
    fpBarSpacing = null;
  }
  if (cfg) connectWs();  // 订阅视图需求并获得完整快照，补齐未观看期间的足迹。
}

function applyFootprint(data, replace = false) {
  if (!data || data.revision < fpRevision) return;
  fpRevision = data.revision;
  const oldLast = fpBars[fpBars.length - 1]?.time;
  const canUpdate = !replace && data.bars.length === 1 && fpBars.length < 800 &&
                    (oldLast == null || data.bars[0].time >= oldLast);
  fpBars = FlowData.mergeBars(replace ? [] : fpBars, data.bars);
  const first = bars[0]?.time;
  if (first != null) fpBars = fpBars.filter((b) => b.time >= first);
  fpSeries.applyOptions({ tickSize: data.tickSize });
  if (view === "footprint") {
    if (canUpdate) fpSeries.update(toFpItem(data.bars[0]));
    else fpSeries.setData(fpBars.map(toFpItem));
  }
  updateLegend(bars.length - 1);
}

function setStatus(ok, text) {
  const el = $("status");
  el.className = ok ? "on" : "off";
  el.textContent = text;
}

// ---------- 数据加载与实时推送 ----------

async function loadHistory(generation = ++loadGeneration) {
  setStatus(false, "加载中…");
  ++wsGeneration;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  clearTimeout(watchdog);
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

function onBars(updates) {
  if (!updates.length) return;
  const oldLast = bars[bars.length - 1]?.time;
  const onlyLast = updates.length === 1 && updates[0].time === oldLast;
  bars = FlowData.mergeBars(bars, updates);
  if (onlyLast) updateLast();
  else renderAll();  // 补齐/修订历史和窗口裁剪时，所有图表使用同一份数据。
  if (view === "footprint" && fpBars.length) {
    const latest = fpBars[fpBars.length - 1];
    if (onlyLast && latest.time === oldLast) fpSeries.update(toFpItem(latest));
    else fpSeries.setData(fpBars.map(toFpItem));
  }
}

function connectWs() {
  const generation = ++wsGeneration;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  clearTimeout(watchdog);
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  setStatus(false, "同步中…");
  const wsScheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${wsScheme}://${location.host}/ws?symbol=${encodeURIComponent(symbol)}&ltf=${ltf}&footprint=${view === "footprint"}`);
  ws = socket;
  let synced = false;
  const armWatchdog = () => {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      if (generation === wsGeneration) socket.close();
    }, synced ? 45000 : 100000);
  };
  socket.onopen = () => {
    if (generation === wsGeneration) armWatchdog();
  };
  socket.onclose = () => {
    if (generation !== wsGeneration) return;
    clearTimeout(watchdog);
    setStatus(false, "已断开, 重连补齐中…");
    reconnectTimer = setTimeout(() => {
      if (generation === wsGeneration) connectWs();
    }, 3000);
  };
  socket.onerror = () => {
    if (generation === wsGeneration) setStatus(false, "连接错误");
  };
  socket.onmessage = (ev) => {
    if (generation !== wsGeneration) return;
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "ping") {
        if (msg.error || msg.status?.status !== "connected") {
          setStatus(false, msg.error || "行情源: " + (msg.status?.lastError || msg.status?.status || "未知"));
        } else if (synced) setStatus(true, "已连接");
      } else if (msg.symbol === symbol && msg.type === "snapshot" && msg.ltf === ltf) {
        cfg = msg.cfg;
        bars = FlowData.mergeBars([], msg.bars);
        barRevision = msg.revision;
        fpRevision = -1;
        renderAll();
        if (msg.footprint) applyFootprint(msg.footprint, true);
        else { fpBars = []; fpSeries.setData([]); }
        synced = true;
        setStatus(true, "已连接");
      } else if (synced && msg.symbol === symbol) {
        if (msg.type === "bars" && msg.ltf === ltf && msg.revision > barRevision) {
          onBars(msg.bars);
          barRevision = msg.revision;
        } else if ((msg.type === "footprints" || msg.type === "footprint_snapshot") && msg.revision > fpRevision) {
          applyFootprint(msg, msg.type === "footprint_snapshot");
        }
      }
      armWatchdog();
    } catch (error) {
      setStatus(false, "数据同步失败: " + error.message);
      socket.close();
    }
  };
}

loadHistory().catch((e) => { setStatus(false, "加载失败: " + e.message); });
