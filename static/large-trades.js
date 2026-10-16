/* 原始成交先筛选、再按 K 线汇总的大额事件。只负责展示, 阈值由服务端下发。 */
(function (root) {
  "use strict";
  const COLORS = { buy: "#30cf9b", sell: "#ff6978" };
  const QUALITY = { complete: "完整", partial: "部分覆盖", warming: "基线预热中", missing: "未采集" };
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  function number(v, digits = 2) {
    return finite(v) ? v.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
  }
  function signed(v, digits = 2) { return finite(v) ? `${v > 0 ? "+" : ""}${number(v, digits)}` : "—"; }
  // 金额门槛: 期货按人民币, 万元起记; 加密按 USDT。
  function money(v, source) {
    if (source !== "snapshot") return `${number(v, 0)} USDT`;
    return v >= 1e4 ? `${number(v / 1e4, 0)} 万元` : `${number(v, 0)} 元`;
  }
  // 一笔刚够门槛的大单折成的数量(期货手、加密币): 金额门槛按参考价格和合约乘数换算。
  // 价格或乘数还不知道时返回 null, 等数据到了再定, 不能锁定错误的单位。
  function floorUnit(configs = [], referencePrice) {
    if (!configs.length) return null;
    const price = finite(referencePrice) && referencePrice > 0 ? referencePrice : null;
    const units = configs.map((c) => {
      const crypto = c.source !== "snapshot";
      const perUnit = crypto ? price : price && finite(c.multiplier) && c.multiplier > 0 ? price * c.multiplier : null;
      if (c.minNotional > 0 && perUnit == null) return null;
      const qty = Math.max(c.minQty || 0, c.minNotional > 0 ? c.minNotional / perUnit : 0);
      return qty > 0 ? qty : crypto ? (price ? 10000 / price : null) : 20;
    });
    return units.every((v) => finite(v) && v > 0) ? Math.min(...units) : null;
  }
  // 每根 K 线买卖两侧各画一个点(买在下、卖在上), 大小按该侧大额量; 对冲的双向大额也看得见。
  const SIDES = ["buy", "sell"];
  function sideVolumes(s) {
    if (!s?.count) return [];
    return SIDES.filter((side) => finite(s[side]) && s[side] > 0).map((side) => [side, s[side]]);
  }
  // 尺寸单位按本图归一: 已加载 K 线各侧大额量的 P90 画成 16px, 品种和周期不同各自成比例
  // (大周期一根汇总的大单多, 单位随之变大, 不会全部顶到上限)。单位不小于一笔门槛大单,
  // 大额量普遍很小时一笔大单仍画 4px; 样本(有大额的买侧、卖侧)不足 SIZE_SAMPLE 个时只用门槛单位。
  const SIZE_SAMPLE = 20;
  function sizeUnit(bars = [], configs = [], referencePrice) {
    const floor = floorUnit(configs, referencePrice);
    const volumes = bars.flatMap((b) => sideVolumes(b?.large).map(([, v]) => v));
    if (volumes.length < SIZE_SAMPLE) return floor;
    volumes.sort((a, b) => a - b);
    const unit = volumes[Math.floor((volumes.length - 1) * 0.9)] / 16;   // radius(P90) = 4·sqrt(16) = 16
    return floor == null ? unit : Math.max(floor, unit);
  }
  // 半径 ∝ sqrt(量), 即面积 ∝ 量; 3–20px 的边界兼顾可见性与遮挡。
  function radius(qty, unit = 20) {
    const base = finite(unit) && unit > 0 ? unit : 20;
    return Math.max(3, Math.min(20, 4 * Math.sqrt(Math.abs(qty || 0) / base)));
  }
  function readout(s, digits) {
    if (!s || ["missing", "warming"].includes(s.coverage)) return QUALITY[s?.coverage] || QUALITY.missing;
    return `Δ ${signed(s.delta, digits)}` +
      ` · CVD ${signed(s.cvd, digits)}${s.coverage === "partial" ? " · 部分" : ""}`;
  }
  function details(bar, configs = [], digits = 0) {
    const s = bar?.large;
    if (!s) return "大额成交 · 未采集\n历史 K 线不能反推出原始大额事件。";
    const crypto = configs.some((c) => c.source !== "snapshot");
    const unit = crypto ? "币" : "手";
    const time = new Date(bar.time * 1000).toISOString().slice(5, 19).replace("T", " ");
    const lines = [`大额成交 · ${time} · ${QUALITY[s.coverage] || QUALITY.missing}`];
    if (!["missing", "warming"].includes(s.coverage)) {
      lines.push(`买 ${number(s.buy, digits)} / 卖 ${number(s.sell, digits)} / 未知 ${number(s.unknown, digits)} ${unit}`,
        `大额 Δ ${signed(s.delta, digits)} · 本次采集 CVD ${signed(s.cvd, digits)}`,
        `${number(s.count, 0)} 次事件 · 最大事件 ${number(s.maxQty, digits)} ${unit}`,
        `最大相对量 ${s.count ? number(s.maxRatio, 2) + "×" : "—"} · 最大 Z ${number(s.maxZ, 2)}`,
        `大额量占比 ${finite(s.share) ? number(s.share * 100, 1) + "%" : s.coverage === "complete"
          ? "—（总量为零或待核对）" : "—（未覆盖整根 K 线）"}`);
    }
    for (const c of configs) {
      const floor = [c.minNotional > 0 ? `≥${money(c.minNotional, c.source)}` : "",
        c.minQty > 0 ? `≥${number(c.minQty, digits)}${unit}` : ""].filter(Boolean).join(" 且 ");
      lines.push(`${c.symbol} · ${c.label}`,
        `${floor ? floor + " 且 " : ""}>基线 P${number(c.quantile * 100, 2)}`,
        `基线：最近 ${number(c.windowSec / 60, 0)} 分钟交易时间的有效事件` +
          `${c.lagSec > 0 ? `（不含最近 ${number(c.lagSec, 0)} 秒）` : ""}，不足 ${c.warmup} 个时往前补足`);
    }
    if (configs.some((c) => c.source === "snapshot")) lines.push("期货按快照区间量和估算方向统计，不代表单个订单。");
    if (crypto) lines.push("加密：同一毫秒、同一方向的连续成交合并成一个事件（近似一张吃单）。");
    if (s.unknown > 0) lines.push("未知方向计入总量与次数，不画点，不计入 Δ / CVD。");
    lines.push("买卖各画一个点：大额买在 K 线下方、大额卖在上方，面积按该侧大额量缩放，按本图近期大额量归一（不同图之间大小不可比）。",
      "CVD 仅累计已采集事件。",
      crypto ? "断档后重新预热；断线、关机期间的缺口回填后补判最近 6 小时，已完整落盘的旧 K 线不回推。"
        : "断档后重新预热；旧 K 线不回推，重启后重新采集。");
    return lines.join("\n");
  }

  // 命中按当前几何现算, 不依赖上一帧画了什么: 数据更新后立即重查悬停, 不必挂在绘制回调里。
  class Primitive {
    constructor(geometry) {
      this.geometry = geometry;
      this._view = { zOrder: () => "top", renderer: () => ({ draw: (target) => this.draw(target) }) };
    }
    attached({ requestUpdate }) { this.requestUpdate = requestUpdate; }
    detached() { this.requestUpdate = null; }
    paneViews() { return [this._view]; }
    refresh() { this.requestUpdate?.(); }
    hit(point) {
      if (!point) return null;
      return this.geometry().find((m) => Math.hypot(m.x - point.x, m.y - point.y) <= m.r + 3) || null;
    }
    draw(target) {
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize: size }) => {
        ctx.save();
        for (const m of this.geometry()) {
          if (![m.x, m.y].every(finite) || m.x < -m.r || m.x > size.width + m.r ||
              m.y < -m.r || m.y > size.height + m.r) continue;
          ctx.beginPath();
          ctx.arc(m.x, m.y, m.r, 0, Math.PI * 2);
          ctx.fillStyle = COLORS[m.side] + "45";
          ctx.fill();
          ctx.fillStyle = COLORS[m.side];
          ctx.font = "bold 10px sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(m.side === "buy" ? "+" : "−", m.x, m.y);
        }
        ctx.restore();
      });
    }
  }
  const api = { COLORS, QUALITY, sideVolumes, floorUnit, sizeUnit, radius, readout, details, Primitive };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowLargeTrades = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
