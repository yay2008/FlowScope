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
  const api = { mergeBars, diagonalVolumes, cvdCandle, splitLtf, relativeLevel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowData = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
