/* 指标计算: 滚动统计、Volume Suite 阈值分档、LSMA × CRVOL 共振、FlowWave 回归通道。
 *
 * 纯函数, 不碰 DOM 与图表, 浏览器与 Node 回归测试共用(与 data-sync.js 同样的写法)。
 * 状态(bars / cfg / threshtype / 带宽 k)全部由调用方传入, 模块内不留全局状态。
 */
(function (root) {
  "use strict";

  // ---------- 参数 ----------

  // LSMA × CRVOL 共振参数(同 Pine 默认值)
  const LW = { n1: 9, n2: 6, n3: 3, n4: 21, ob: 80, os: 20, slopeLen: 10 };

  // 主图叠加的回归通道参数: 中线 = linreg(close, n), 上下轨 = 中线 ± k 倍回归残差标准差。
  // 残差标准差取与中线同一个窗口, 用总体标准差(除以 n)。k 在主图图例里点「2σ」按 kOptions 的顺序循环切换,
  // 两张图共用; 三档包含率与选型依据见 docs/flowwave_band_probe.py。
  const BAND = { n: 21, kOptions: [1.5, 2, 2.5], kDefault: 2 };

  // 主图 EMA 周期
  const EMA_PERIODS = [21, 55, 100, 200];

  // ---------- 判向口径 ----------
  // 后端对同一根 bar 并列输出两套量: buy/sell/unknown 是新算法(Lee-Ready),
  // buyLegacy/sellLegacy 是旧算法。前端一律读新算法: 旧算法(快照自身盘口)与当根 K 线
  // 方向一致率只有 ~33%(系统性反向, 见 docs/indicator-accuracy-2026-09-16-addendum.md),
  // 所以工具栏的"判向算法"开关已移除。CVD 本来就恒按新算法累计。
  const buyOf = (b) => b.buy ?? null;
  const sellOf = (b) => b.sell ?? null;
  const deltaOf = (b) => b.delta ?? null;

  // ---------- 基础序列函数 ----------

  // 滚动均值/标准分: 窗口里只要有一根是 null(coverage=missing 的买卖量、预热期的相对值)就输出 null,
  // 与 ta.sma / ta.stdev 的口径一致。缺失不能当 0: 缺口之后的窗口均值会被拉低, 紧跟缺口的 bar
  // 就被判成高档位(实测 SMA 模式缺口后 60 根全部 ≥2 档)。代价是缺口之后要等满一个窗口才重新出档位。
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
    let s = 0, s2 = 0, bad = 0;
    for (let i = 0; i < v.length; i++) {
      if (v[i] == null) bad++; else { s += v[i]; s2 += v[i] * v[i]; }
      if (i >= n) {
        const y = v[i - n];
        if (y == null) bad--; else { s -= y; s2 -= y * y; }
      }
      if (i >= n - 1 && bad === 0) {
        const mean = s / n;
        const variance = Math.max(s2 / n - mean * mean, 0);
        const sd = Math.sqrt(variance);
        out[i] = sd === 0 ? null : (v[i] - mean) / sd;
      }
    }
    return out;
  }

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

  const smaStrict = rollingSma;     // ta.sma: 窗口含 null 则结果为 null, 与 rollingSma 同一口径

  // LazyBear 通道指数: (src - ema(src,n)) / (k * ema(|src - ema(src,n)|, n))。
  // LSMA × CRVOL 的 tci 用 k=0.025。分母为 0 时输出 null(同 Pine 除零得 na):
  // ema 首根播种成自身, 所以第一根的偏离恒为 0, 振荡值从第二根才开始有。
  function channelIndex(src, n, k) {
    const esa = ema(src, n);
    const dev = src.map((x, i) => (x == null || esa[i] == null ? null : x - esa[i]));
    const d = ema(dev.map((x) => (x == null ? null : Math.abs(x))), n);
    return dev.map((x, i) => (x == null || !d[i] ? null : x / (k * d[i])));
  }

  const hlc3Of = (bars) => bars.map((b) => (b.high + b.low + b.close) / 3);

  // ---------- LSMA × CRVOL 共振 ----------

  // crv: derive 里算好的 CRVOL 累计序列(斜率复用它)
  function deriveLw(bars, crv) {
    const n = bars.length;
    const hlc3 = hlc3Of(bars);
    const vol = bars.map((b) => b.volume || 0);

    // tci = ema(channelIndex(src, n1, 0.025), n2) + 50
    const tci = ema(channelIndex(hlc3, LW.n1, 0.025), LW.n2).map((x) => (x == null ? null : x + 50));

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

    // CRVOL 斜率(数据窗口输出): linreg(crvol, slopeLen) 的一阶差分
    const reg = linreg(crv, LW.slopeLen);
    const crvSlope = reg.map((x, i) => (x == null || i === 0 || reg[i - 1] == null ? null : x - reg[i - 1]));

    return { wave, wt2, crvSlope };
  }

  // FlowWave 主图叠加: 价格回归通道。
  // 为什么不能直接把 wave/wt2 画到主图: 它们是 0~100 的振荡值(实测还会溢出到 -14~108), 没有价格量纲,
  // 画到价格轴上必须选一种映射。这里选"轨道由价格自证"的映射 —— 中线仍用同一个 linreg 核, 只把输入
  // 从振荡值换成收盘价, 带宽用回归残差标准差; 于是轨道本身是真实价格(可当动态支撑/压力),
  // 而 wt2 的信息转成带的着色状态(state): 超买/超卖的 bar 在图上染色(画法见 chart-view.js 的 BandRenderer)。
  function deriveBand(bars, wt2, k) {
    const n = bars.length;
    const close = bars.map((b) => b.close);
    const mid = linreg(close, BAND.n);
    const up = new Array(n).fill(null);
    const dn = new Array(n).fill(null);
    const state = new Array(n).fill(0);   // 1=超买(wt2>80), -1=超卖(wt2<20), 0=中性
    for (let i = 0; i < n; i++) {
      if (mid[i] == null) continue;
      let sum = 0, sum2 = 0, ok = true;
      for (let j = i - BAND.n + 1; j <= i; j++) {
        if (mid[j] == null) { ok = false; break; }
        const r = close[j] - mid[j];
        sum += r;
        sum2 += r * r;
      }
      if (!ok) continue;
      const mean = sum / BAND.n;
      const sd = Math.sqrt(Math.max(sum2 / BAND.n - mean * mean, 0));
      if (sd === 0) continue;   // 21 根完全贴在回归线上(极端平滑/停板), 带宽为 0 时不画
      up[i] = mid[i] + k * sd;
      dn[i] = mid[i] - k * sd;
      state[i] = wt2[i] == null ? 0 : wt2[i] > LW.ob ? 1 : wt2[i] < LW.os ? -1 : 0;
    }
    return { mid, up, dn, state };
  }

  // ---------- Volume Suite 派生序列与分档 ----------

  // cfg: 后端下发的 mult/rellen/smalen/zlen; bandK: 叠加带的带宽倍数
  function derive(bars, cfg, bandK) {
    const n = bars.length;
    const vol = bars.map((b) => b.volume);
    const buy = bars.map((b) => buyOf(b));
    const sell = bars.map((b) => sellOf(b));
    const delta = bars.map((b) => deltaOf(b));
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

    const derived = { vol, buy, sell, delta, posd, negd, smaVolN, rvol, rpos, rneg, rbuy, rsell,
                      zVol, zRpos, zRneg, zBuy, zSell, crv, emaLines };
    derived.lw = deriveLw(bars, crv);
    derived.band = deriveBand(bars, derived.lw.wt2, bandK);
    return derived;
  }

  // data-sync.js 在浏览器里挂全局 FlowData, 在 Node 里要 require; 用到时才取, 加载顺序无关
  function flowData() {
    return root.FlowData || require("./data-sync.js");
  }

  // level: 0=未超阈值, 1..3=超过第 1..3 档
  // SMA 模式要 smalen 窗口的均值序列, 首次用到时算一次挂在 derived 上;
  // derived 每次 derive() 都是新对象, 缓存随之作废, 调用方不必手工清。
  function smaNSeries(derived, cfg, kind) {
    if (!derived.smaN) {
      derived.smaN = {
        vol: rollingSma(derived.vol, cfg.smalen),
        posd: rollingSma(derived.posd, cfg.smalen),
        negd: rollingSma(derived.negd, cfg.smalen),
        buy: rollingSma(derived.buy, cfg.smalen),
        sell: rollingSma(derived.sell, cfg.smalen),
      };
    }
    return derived.smaN[kind];
  }

  function levelOf(derived, cfg, threshtype, kind, i) {
    const m = cfg.mult;
    const d = derived;
    const ge = (x, t) => x != null && t != null && x >= t;
    if (threshtype === "RELATIVE") {
      const rel = { vol: d.rvol, posd: d.rpos, negd: d.rneg, buy: d.rbuy, sell: d.rsell }[kind][i];
      return flowData().relativeLevel(kind, rel, m);
    }
    if (threshtype === "SMA") {
      const base = { vol: d.vol, posd: d.posd, negd: d.negd, buy: d.buy, sell: d.sell }[kind][i];
      const smaN = smaNSeries(d, cfg, kind)[i];
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

  const api = {
    LW, BAND, EMA_PERIODS, buyOf, sellOf, deltaOf,
    rollingSma, rollingZ, ema, rma, rsi, linreg, smaStrict, channelIndex,
    deriveLw, deriveBand, derive, levelOf,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FlowIndicators = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
