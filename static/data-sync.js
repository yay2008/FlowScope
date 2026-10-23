/* 可在浏览器和 Node 回归测试中复用的数据合并规则。 */
(function (root) {
  "use strict";
  function mergeBars(existing, updates, limit = Infinity) {
    const byTime = new Map(existing.map((bar) => [bar.time, bar]));
    for (const bar of updates) byTime.set(bar.time, bar);
    const bars = [...byTime.values()].sort((a, b) => a.time - b.time);
    return Number.isFinite(limit) ? bars.slice(-limit) : bars;
  }
  // 重连快照与已有的 K 线接不上(已有的最后一根早于快照第一根): 断开期间缺了一段, 只能整份换掉,
  // 合并的话指标会把缺口两边当成连续的数据来算, 翻页(只往最早一根之前取)也补不上这个洞。
  function snapshotGap(existing, snapshot) {
    return existing.length > 0 && snapshot.length > 0 && existing[existing.length - 1].time < snapshot[0].time;
  }
  function mergeCvdHistory(existing, history, liveFrom) {
    const anchor = history.find((bar) => bar.time === liveFrom);
    let cumulative = anchor?.cvdOpen ?? (anchor?.cvd != null && anchor?.delta != null ? anchor.cvd - anchor.delta : null);
    const live = existing.filter((bar) => bar.time >= liveFrom).map((bar) => {
      if (cumulative == null) return bar;
      if (bar.delta == null) return { ...bar, cvdOpen: null, cvd: null };
      const open = cumulative;
      cumulative += bar.delta;
      return { ...bar, cvdOpen: open, cvd: cumulative };
    });
    // 历史重算覆盖已收盘的根; 末根和等候期间新到的根保留实时成交, 只调整累计基准。
    return mergeBars(mergeBars(existing, history.filter((bar) => bar.time < liveFrom)), live);
  }
  function diagonalVolumes(levels, tickSize) {
    if (!(tickSize > 0)) return levels.map(() => null);
    const byPrice = new Map(levels.map((lv) => [Math.round(lv[0] / tickSize), lv]));
    return levels.map((lv) => {
      const key = Math.round(lv[0] / tickSize);
      return { sellBelow: byPrice.get(key - 1)?.[2] || 0, buyAbove: byPrice.get(key + 1)?.[1] || 0 };
    });
  }
  // 足迹再合档: levels 的价格都是 step 的整数倍、标在本档最低价; 相邻 factor 档并成一档, 仍标在最低价。
  function footprintRows(levels, step, factor) {
    if (!(factor > 1) || !(step > 0)) return levels;
    const rows = new Map();
    for (const [price, buy, sell, unknown = 0] of levels) {
      const key = Math.floor(Math.round(price / step) / factor);
      const row = rows.get(key);
      if (row) { row[1] += buy; row[2] += sell; row[3] += unknown; }
      else rows.set(key, [key, buy, sell, unknown]);
    }
    const size = step * factor;
    return [...rows.values()].sort((a, b) => a[0] - b[0])
      .map(([key, buy, sell, unknown]) => [Number((key * size).toPrecision(12)), buy, sell, unknown]);
  }
  // 显示档(最小价位的倍数, base 是底档): 候选是 1-2-2.5-5 序列里能被底档整除的整数价位,
  // 再补上底档的 1-2-5 倍 —— 底档是 2 个价位时只靠前者会从 2 直接跳到 10, 补上 4 以后相邻候选最多差 2.5 倍;
  // 底档不在序列上(多所公约数之类)时也有候选。取不小于 needed 的最小一个。
  function footprintMultiple(base, needed) {
    const options = [];
    for (let magnitude = 1; magnitude <= 1e9; magnitude *= 10) {
      for (const n of [1, 2, 2.5, 5]) {
        const ticks = n * magnitude;
        if (Number.isInteger(ticks) && ticks % base === 0) options.push(ticks);
        if (n !== 2.5) options.push(n * magnitude * base);
      }
    }
    return options.sort((a, b) => a - b).find((ticks) => ticks >= needed) ?? Math.ceil(needed / base) * base;
  }
  function footprintThresholds(bars) {
    // 足迹不平衡的优势侧最小量(期货与加密共用，期货另有手数下限，见 chart-view.js FP.imbMinLots)。
    // 每根的非零买/卖半格均量代表其典型档量；此前最多 20 根完整足迹取中位数，
    // 隔离品种、周期及显示分档的量级，也避免单根放量抬高整段门槛。
    // bars 按时间升序，当前根先取门槛再入基线，不使用未来量；首根用自身均量启动。
    const thresholds = new Map(), samples = [];
    for (const bar of bars) {
      let mean = 0, count = 0;
      for (const level of bar.levels || []) {
        for (const volume of [level[1], level[2]]) {
          if (Number.isFinite(volume) && volume > 0) mean += (volume - mean) / ++count;
        }
      }
      const sorted = [...samples].sort((a, b) => a - b);
      const mid = sorted.length >> 1;
      const minimum = sorted.length
        ? (sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2)
        : count ? mean : null;
      thresholds.set(bar.time, minimum);
      if (bar.coverage === "complete" && count) {
        samples.push(mean);
        if (samples.length > 20) samples.shift();
      }
    }
    return thresholds;
  }
  function cvdCandle(bar) {
    if (bar.cvd == null || bar.delta == null) return null;
    const open = bar.cvdOpen ?? bar.cvd - bar.delta;
    return { time: bar.time, open, high: Math.max(open, bar.cvd),
             low: Math.min(open, bar.cvd), close: bar.cvd };
  }
  function splitLtf(source, seconds) {
    return source === "tick" ? 0 : [1, 5, 10, 15, 30].includes(Number(seconds)) ? Number(seconds) : 10;
  }
  function relativeLevel(kind, ratio, multipliers) {
    if (ratio == null) return 0;
    const delta = kind === "posd" || kind === "negd";
    for (let i = multipliers.length - 1; i >= 0; i--) {
      const threshold = multipliers[i] * (delta ? 1.5 : 1);
      if (delta ? ratio > threshold : ratio >= threshold) return i + 1;
    }
    return 0;
  }
  // 合约选择器的联动依据: KQ.m@SHFE.fu 是主力(主连), SHFE.fu2611 是该品种的具体月份。
  // 指数(KQ.i@...)、外盘等认不出来的代码返回 null, 由调用方退化成「自定义」。
  function parseSymbolParts(raw) {
    const cont = /^KQ\.m@([A-Za-z]+)\.([A-Za-z0-9]+)$/.exec(raw || "");
    if (cont) return { exchange: cont[1].toUpperCase(), product: cont[2], isCont: true };
    const month = /^([A-Za-z]+)\.([A-Za-z]+)\d{0,4}$/.exec(raw || "");
    if (month) return { exchange: month[1].toUpperCase(), product: month[2], isCont: false };
    return null;
  }
  // 持仓量/成交量的量级缩写。持仓量在服务端已归一成单边口径(见 catalog.single_side_open_interest),
  // 所以这里的数字与自选面板的实时持仓量可比。
  function formatOpenInterest(value) {
    const n = Number(value) || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
    return String(Math.round(n));
  }
  // 当日累计成交额(元)压成「23.1亿」这种便于一眼比较的量级写法。
  function formatAmount(value) {
    if (value == null) return "";
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n >= 1e8) return (n / 1e8).toFixed(1) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
    return String(Math.round(n));
  }
  // 自选面板第三行: 当日成交量 + 成交额。两个都取不到就不占位置。
  function liquidityLabel(row) {
    if (!row) return "";
    const parts = [];
    if (row.volume != null) parts.push(`量 ${formatOpenInterest(row.volume)}`);
    const amount = formatAmount(row.amount);
    if (amount) parts.push(`额 ${amount}`);
    return parts.join(" · ");
  }
  // 自选面板的一行: 主连写成「品种 · 主力月份」, 具体月份直接用合约中文名。
  function watchLabel(row) {
    const name = (row && (row.name || row.symbol)) || "";
    if (!row || row.insClass !== "CONT") return name;
    const base = name.replace(/主连$/, "");
    const month = String(row.mainSymbol || "").split(".")[1];
    return month ? `${base} · ${month}` : base || name;
  }
  function formatPrice(value, decimals) {
    if (value == null) return "—";
    return Number(value).toFixed(Number(decimals) || 0);
  }
  function formatChangePct(pct) {
    if (pct == null) return "";
    return `${pct > 0 ? "+" : ""}${Number(pct).toFixed(2)}%`;
  }
  function changeClass(pct) {
    if (pct == null || pct === 0) return "";
    return pct > 0 ? "up" : "down";
  }
  // 合约选择器的一行: 品种写成「燃油 · fu2611」(带当前主力月份), 月份写成「燃油2611 · 昨仓 42.8万」。
  // 「昨仓」是单边口径的昨日持仓量, 与自选面板的实时持仓量(同为单边)可以直接比大小。
  function productLabel(product) {
    const name = (product && (product.name || product.productId)) || "";
    const month = String((product && product.mainSymbol) || "").split(".")[1] || "";
    return month ? `${name} · ${month}` : name;
  }
  function monthLabel(option) {
    const name = (option && (option.name || option.symbol)) || "";
    if (!option || option.openInterest == null) return name;
    return `${name} · 昨仓 ${formatOpenInterest(option.openInterest)}`;
  }
  const api = { mergeBars, snapshotGap, mergeCvdHistory, diagonalVolumes, footprintRows, footprintMultiple, footprintThresholds,
                cvdCandle, splitLtf, relativeLevel,
                parseSymbolParts, formatOpenInterest, formatAmount, liquidityLabel,
                watchLabel, formatPrice, formatChangePct, changeClass, productLabel, monthLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowData = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
