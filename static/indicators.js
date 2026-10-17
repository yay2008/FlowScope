/* 指标计算: 滚动统计、Volume Suite 阈值分档、LSMA × CRVOL 共振、FlowWave 回归通道、OB Zones、固定范围成交量分布。
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
  // 主图叠加的大周期 EMA 周期(在本图上画更大周期的 EMA, 见 htfEma)
  const HTF_EMA_PERIODS = [21, 55];

  // OB Zones.pine 默认值。阈值固定为 4; 一组区域对应 Pine 的三个 box, 最多显示 166 组完整区域。
  const OB = { zLen: 100, maxAge: 500, preventOverlap: true,
               bullColor: "#00ffbb", bearColor: "#ff1100", maxDrawZones: 166 };

  function normalizeObOptions(value = {}) {
    const v = value && typeof value === "object" ? value : {};
    const integer = (key) => Number.isSafeInteger(v[key]) && v[key] >= 1 ? v[key] : OB[key];
    const color = (key) => typeof v[key] === "string" && /^#[\da-f]{6}$/i.test(v[key]) ? v[key] : OB[key];
    return { zLen: integer("zLen"), maxAge: integer("maxAge"),
             preventOverlap: typeof v.preventOverlap === "boolean" ? v.preventOverlap : OB.preventOverlap,
             bullColor: color("bullColor"), bearColor: color("bearColor") };
  }

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

  // 大周期分桶: t 落在哪根大周期 bar 里, 返回它的起点。starts 是大周期图已知各根的起点(升序), 按大周期图自己的切法分
  // (期货大周期是 TqSdk 原生 K 线, 不假设它怎么对齐); 越过最近那根的跨度(还没有的新 bar、休市之后)或早于第一根时,
  // 从最近那根的起点(没有就从 0)按 htf 的整数倍往后推, 即按自然时间对齐, 同加密大周期的合成
  function htfBucket(starts, htf, t) {
    let lo = 0, hi = starts.length - 1, k = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (starts[mid] <= t) { k = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const origin = k < 0 ? 0 : starts[k];
    return origin + Math.floor((t - origin) / htf) * htf;
  }

  // 大周期 EMA 画在本图上: 本图 bar 按大周期分桶(htfBucket), 桶的收盘价 = 桶里最后一根的收盘价, 在这些收盘价上算 EMA
  // (同 ema, 首个收盘价播种)。htfBars 是大周期图的 bar [{time, close}](升序, 可省略): 定分桶的起点, 本图窗口之前的桶
  // 用它们的收盘价接着算 —— 页面每张图只有最近 800 根, 10s 折成 1m 才 133 根, 不靠它 EMA55 在窗口里大半段都在预热。
  // 窗口里的桶以本图为准(大周期图的最后一根拿到时还没收盘)。
  // 画回本图时每根 bar 取最近一根已收盘的大周期 bar 的 EMA: 大周期这一根还没走完时一直是上一根的值, 走完了
  // (本图出现下一桶的第一根)才更新, 画成阶梯; 相当于大周期图的 EMA 往右挪一根。历史与实时一致, 不会事后重画。
  // 窗口里第一个桶之前既没有大周期图的 bar 也没有本图的 bar 时, 那个桶里还没有值(null)。
  // 返回 {lines, buckets}: lines 与 periods 一一对应, 每条与 bars 等长(htf 不是正数时全为 null); buckets 是每根所在桶的起点。
  function htfEma(bars, htf, periods, htfBars = []) {
    const lines = periods.map(() => new Array(bars.length).fill(null));
    const buckets = new Array(bars.length).fill(null);
    if (!(htf > 0)) return { lines, buckets };
    const starts = htfBars.map((b) => b.time);
    const alpha = periods.map((p) => 2 / (p + 1));
    const step = (prev, close, j) => (prev == null ? close : alpha[j] * close + (1 - alpha[j]) * prev);
    const closed = periods.map(() => null);   // 最近一根已收盘的大周期 bar 的 EMA
    let bucket = bars.length ? htfBucket(starts, htf, bars[0].time) : null;
    for (const b of htfBars) {                // 本图窗口之前的桶: 接着大周期图的收盘价算
      if (bucket == null || b.time >= bucket) break;
      if (b.close != null) periods.forEach((p, j) => { closed[j] = step(closed[j], b.close, j); });
    }
    let last = null;                          // 当前桶到这根为止最后的收盘价, 桶走完时就是桶的收盘价
    for (let i = 0; i < bars.length; i++) {
      const start = htfBucket(starts, htf, bars[i].time);
      if (start !== bucket) {                 // 进了新桶: 上一桶已收盘, 用它的收盘价把 EMA 往前推一根
        if (last != null) periods.forEach((p, j) => { closed[j] = step(closed[j], last, j); });
        bucket = start;
        last = null;
      }
      buckets[i] = start;
      if (bars[i].close != null) last = bars[i].close;
      periods.forEach((p, j) => { lines[j][i] = closed[j]; });
    }
    return { lines, buckets };
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

  // ---------- OB Zones (TradingView Pine v6 移植) ----------

  // 只用截至当前 bar 的 OHLC 产生信号。区域回画到最近一根反向 K 线, created 才是真正触发的 bar。
  // 成交量改用本项目 buy/sell(跟随顶部拆分粒度), 缺失保留 null, 不按整根阴阳伪造买卖量。
  // 全量重建避免实时末根回撤、历史补齐或窗口裁剪后留下旧区域, 与 Pine 的实时 rollback 行为一致。
  function deriveObZones(bars, options = {}) {
    const { zLen, maxAge, preventOverlap } = normalizeObOptions(options);
    const n = bars.length;
    const up = new Array(n), down = new Array(n);
    for (let i = 0; i < n; i++) {
      const body = bars[i].close - bars[i].open;
      up[i] = body > 0 ? (i ? up[i - 1] : 0) + body : 0;
      down[i] = body < 0 ? (i ? down[i - 1] : 0) - body : 0;
    }
    // Pine ta.stdev 默认总体标准差(除以 n); 标准差为零时 Z 为 na。
    const zUp = obZ(up, zLen), zDn = obZ(down, zLen);
    const bullCreate = new Array(n).fill(false), bearCreate = new Array(n).fill(false);
    const zones = [];
    let active = [], lastDown = null, lastUp = null;
    const crossed = (z, i) => i > 0 && z[i] != null && z[i - 1] != null &&
      z[i] > 4 && z[i - 1] <= 4 && z[i - 1] !== 0;
    const create = (start, i, side) => {
      if (start == null) return false;
      const b = bars[start], top = b.high, bottom = b.low;
      if (!Number.isFinite(top) || !Number.isFinite(bottom) || top < bottom) return false;
      // 严格相交才挡住, 边界相接允许; 多空区域互相检查。
      if (preventOverlap && active.some((z) => top > z.bottom && bottom < z.top)) return false;
      const volume = (v) => Number.isFinite(v) && v >= 0 ? v : null;
      const zone = { side, start, created: i, end: i, top, bottom, status: "active",
                     buy: volume(buyOf(b)), sell: volume(sellOf(b)), unknown: volume(b.unknown),
                     coverage: b.coverage ?? null };
      zones.push(zone);
      active.push(zone);
      return true;
    };
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      if (b.close < b.open) lastDown = i;
      if (b.close > b.open) lastUp = i;
      // 原脚本先创建再失效: 在本根到期/突破的旧区域仍会阻止本根的重叠新区域。
      if (crossed(zUp, i)) bullCreate[i] = create(lastDown, i, "bull");
      if (crossed(zDn, i)) bearCreate[i] = create(lastUp, i, "bear");
      active = active.filter((z) => {
        z.end = i;
        const previous = i ? bars[i - 1].close : null;
        const mitigated = previous != null && (z.side === "bull"
          ? b.close < z.bottom && previous < z.bottom : b.close > z.top && previous > z.top);
        const expired = i - z.start >= maxAge;   // 从反向 K 线起算, 不是从信号根起算
        if (mitigated || expired) z.status = mitigated ? "mitigated" : "expired";
        return z.status === "active";
      });
    }
    // Pine 只 array.remove, 没有 box.delete: 失效区域留在历史上, 右端停在失效的 bar。
    return { zones, zUp, zDn, bullCreate, bearCreate };
  }

  // rollingZ 用累加再减的滚动和: 一段连续阴线(距离全为 0)之后浮点残差会让标准差略大于 0,
  // 前一根 Z 成了极小的非零数而不是 na, 下一根阳线就被误判为上穿。窗口内各值完全相同时
  // 真实标准差恰为 0, 按 Pine 记为 na。
  function obZ(values, n) {
    const z = rollingZ(values, n);
    let same = 0;   // 截至本根, 连续与前一根相等的根数
    for (let i = 0; i < values.length; i++) {
      same = i > 0 && values[i] === values[i - 1] ? same + 1 : 0;
      if (same >= n - 1) z[i] = null;
    }
    return z;
  }

  // 成交量条与 Pine 一样: 较长的一条到区域中点, 另一条按买卖比例缩短, 端点按 bar 下标向下取整。
  function obVolumeEnds(zone) {
    if (zone.buy == null || zone.sell == null) return null;
    const mid = Math.floor((zone.start + zone.end) / 2);
    const ratio = zone.buy / Math.max(zone.sell, 1e-7);
    return ratio > 1
      ? { buy: mid, sell: Math.floor(zone.start + (mid - zone.start) / ratio) }
      : { buy: Math.floor(zone.start + (mid - zone.start) * ratio), sell: mid };
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

  // ---------- 固定范围成交量分布(Fixed Range Volume Profile) ----------
  // 口径同 TradingView 的 FRVP: 区间内的价格从最低到最高切成若干行(默认 24 行), 每根 K 线的成交量按它的
  // 最低-最高价均匀摊进各行。TradingView 用小周期 K 线、按阴阳分上涨/下跌量; 这里直接用本图的 K 线(周期越大
  // 价位分布越粗), 方向用逐笔判出的主动买/卖量, 判不出方向的部分(期货的未知量、没有买卖量的 bar)记为未知。
  // 给了价格步长 step(最小价位)时按价位格子算: 一根 bar 覆盖最低到最高之间的每个价位, 每个价位分到一样多的量;
  // 每行是整数个价位(同 TradingView 按最小变动价位取整), 每行几个价位取「实际行数离要求的行数更近」的那个,
  // 所以实际行数可能和要求的差一两行。没给 step 时按连续价格均匀切。
  // POC = 总量最多的行(并列取离中间近的, 再并列取上面的)。价值区按 TradingView 文档的步骤: 从 POC 开始,
  // 每次比较上下两侧紧挨着的下一行, 取量大的(相等取离 POC 近的, 再相等取上面的); 加上它会超过目标量
  // (总量 × 70%)就停, 所以价值区的量不超过目标量。一侧到头了只看另一侧。
  const VP = { rowOptions: [12, 24, 48, 96], rowsDefault: 24, valueArea: 0.7 };

  // bars[from..to](含两端)的成交量分布; 区间里没有 bar 时返回 null。
  // 行按价格升序: {lo, hi, price, buy, sell, unknown, total}, lo/hi 是行的上下沿(画图用), price 是行中间的价格。
  // poc / vaLow / vaHigh 是行下标; pocPrice 是 POC 行中间的价格, vah / val 是价值区最高 / 最低的价位。
  function volumeProfile(bars, from, to, { rows = VP.rowsDefault, valueArea = VP.valueArea, step = 0 } = {}) {
    const first = Math.max(from, 0), last = Math.min(to, bars.length - 1);
    let top = -Infinity, bottom = Infinity;
    for (let i = first; i <= last; i++) {
      const b = bars[i];
      if (!Number.isFinite(b.high) || !Number.isFinite(b.low)) continue;
      top = Math.max(top, b.high);
      bottom = Math.min(bottom, b.low);
    }
    if (!(top >= bottom)) return null;
    // 统一换成「格」坐标: 有 step 时一个价位 p 占 [p/step - 0.5, p/step + 0.5], 一根 bar 就是一段连续区间,
    // 与行求重叠长度就等于数价位; 没有 step 时就是价格本身。
    const unit = step > 0 ? step : 1;
    const pad = step > 0 ? 0.5 : 0;
    const toUnit = (p) => (step > 0 ? Math.round(p / step) : p);
    const base = toUnit(bottom) - pad;
    const span = toUnit(top) + pad - base;
    let height;
    if (step > 0) {
      const k1 = Math.max(Math.floor(span / rows), 1), k2 = k1 + 1;
      height = Math.abs(Math.ceil(span / k2) - rows) < Math.abs(Math.ceil(span / k1) - rows) ? k2 : k1;
    } else {
      height = span / rows;
    }
    const n = height > 0 ? Math.ceil(span / height - 1e-9) : 1;
    const out = Array.from({ length: n }, (_, j) => {
      const lo = (base + j * height) * unit, hi = (base + (j + 1) * height) * unit;
      return { lo, hi, price: (lo + hi) / 2, buy: 0, sell: 0, unknown: 0, total: 0 };
    });
    const rowOf = (u) => Math.min(Math.max(height > 0 ? Math.floor((u - base) / height) : 0, 0), n - 1);
    let count = 0;
    for (let i = first; i <= last; i++) {
      const b = bars[i];
      const volume = b.volume;
      if (!(volume > 0) || !Number.isFinite(b.high) || !Number.isFinite(b.low)) continue;
      count += 1;
      // 主动买/卖占这根量的比例; 买卖量之和比成交量还大(取整误差)时按买卖量之和算, 没有未知量
      const buy = buyOf(b), sell = sellOf(b);
      const known = Number.isFinite(buy) && Number.isFinite(sell) ? buy + sell : 0;
      const whole = Math.max(volume, known);
      const buyShare = known ? buy / whole : 0, sellShare = known ? sell / whole : 0;
      const add = (j, v) => {
        const row = out[j];
        row.buy += v * buyShare;
        row.sell += v * sellShare;
        row.unknown += v * (1 - buyShare - sellShare);
        row.total += v;
      };
      const a = toUnit(b.low) - pad, z = toUnit(b.high) + pad;
      if (z <= a) {           // 没有 step 时最高 = 最低: 整根落进那一行
        add(rowOf(a), volume);
        continue;
      }
      for (let j = rowOf(a), jEnd = rowOf(z - 1e-9 * (z - a)); j <= jEnd; j++) {
        const overlap = Math.min(z, base + (j + 1) * height) - Math.max(a, base + j * height);
        if (overlap > 0) add(j, volume * overlap / (z - a));
      }
    }
    let total = 0, buy = 0, sell = 0, maxTotal = 0, poc = 0;
    const middle = (n - 1) / 2;
    for (let j = 0; j < n; j++) {
      const row = out[j];
      total += row.total;
      buy += row.buy;
      sell += row.sell;
      if (row.total > maxTotal ||
          (row.total === maxTotal && Math.abs(j - middle) <= Math.abs(poc - middle))) {
        maxTotal = row.total;
        poc = j;
      }
    }
    // 价值区: 从 POC 往两侧扩, 加上下一行会超过目标量就停(见上方说明)
    const target = total * valueArea * (1 + 1e-12);
    let acc = out[poc].total, vaLow = poc, vaHigh = poc;
    for (;;) {
      const above = vaHigh + 1 < n ? out[vaHigh + 1].total : null;
      const below = vaLow > 0 ? out[vaLow - 1].total : null;
      if (above == null && below == null) break;
      const up = below == null ? true : above == null ? false
        : above !== below ? above > below : vaHigh + 1 - poc <= poc - (vaLow - 1);
      const v = up ? above : below;
      if (acc + v > target) break;
      acc += v;
      if (up) vaHigh += 1; else vaLow -= 1;
    }
    return {
      rows: out, poc, vaLow, vaHigh, total, buy, sell, maxTotal, count,
      pocPrice: out[poc].price, vah: out[vaHigh].hi - pad * unit, val: out[vaLow].lo + pad * unit,
    };
  }

  const api = {
    LW, BAND, EMA_PERIODS, HTF_EMA_PERIODS, OB, VP, buyOf, sellOf, deltaOf,
    rollingSma, rollingZ, ema, htfBucket, htfEma, rma, rsi, linreg, smaStrict, channelIndex,
    deriveLw, deriveBand, deriveObZones, normalizeObOptions, obVolumeEnds, derive, levelOf, volumeProfile,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FlowIndicators = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
