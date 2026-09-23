/* 可在浏览器和 Node 回归测试中复用的数据合并规则。 */
(function (root) {
  "use strict";
  function mergeBars(existing, updates, limit = 800) {
    const byTime = new Map(existing.map((bar) => [bar.time, bar]));
    for (const bar of updates) byTime.set(bar.time, bar);
    return [...byTime.values()].sort((a, b) => a.time - b.time).slice(-limit);
  }
  function diagonalVolumes(levels, tickSize) {
    if (!(tickSize > 0)) return levels.map(() => null);
    const byPrice = new Map(levels.map((lv) => [Math.round(lv[0] / tickSize), lv]));
    return levels.map((lv) => {
      const key = Math.round(lv[0] / tickSize);
      return { sellBelow: byPrice.get(key - 1)?.[2] || 0, buyAbove: byPrice.get(key + 1)?.[1] || 0 };
    });
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
  const api = { mergeBars, diagonalVolumes, cvdCandle, splitLtf, relativeLevel,
                parseSymbolParts, formatOpenInterest, formatAmount, liquidityLabel,
                watchLabel, formatPrice, formatChangePct, changeClass, productLabel, monthLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowData = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
