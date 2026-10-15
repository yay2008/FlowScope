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
  // 用绝对规模门槛作数量单位, 在图表首次取得有效数据时固定, 不随每根价格或可视范围变化。
  // 加密金额门槛按同一个参考价格换成币, 避免小数币数量的圆点全挤在最小尺寸。
  function sizeUnit(configs = [], referencePrice) {
    if (!configs.length) return null;
    const units = configs.map((c) => {
      const crypto = c.source !== "snapshot";
      if (crypto && !(finite(referencePrice) && referencePrice > 0)) return null;
      const qty = Math.max(c.minQty || 0, finite(referencePrice) && referencePrice > 0
        ? (c.minNotional || 0) / referencePrice : 0);
      return qty > 0 ? qty : crypto ? 10000 / referencePrice : 20;
    });
    return units.every((v) => finite(v) && v > 0) ? Math.min(...units) : null;
  }
  // 半径 ∝ sqrt(|Delta|), 即面积 ∝ |Delta|; 3–20px 的边界兼顾可见性与遮挡。
  function radius(delta, unit = 20) {
    const base = finite(unit) && unit > 0 ? unit : 20;
    return Math.max(3, Math.min(20, 4 * Math.sqrt(Math.abs(delta || 0) / base)));
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
      const floor = [c.minQty > 0 ? `≥${number(c.minQty, digits)}${unit}` : "",
        c.minNotional > 0 ? `≥${number(c.minNotional, 0)} USDT` : ""].filter(Boolean).join(" 且 ");
      lines.push(`${c.symbol} · ${c.label}`,
        `${floor ? floor + " 且 " : ""}>P${number(c.quantile * 100, 2)} 且 ≥均量${number(c.minRatio)}倍`,
        `基线：前 ${c.window} 个有效事件，至少 ${c.warmup} 个预热`);
    }
    if (configs.some((c) => c.source === "snapshot")) lines.push("期货按快照区间量和估算方向统计，不代表单个订单。");
    if (s.unknown > 0) lines.push("未知方向计入总量与次数，不计入 Δ / CVD。");
    lines.push("每根 K 线一个净方向点，面积按 |大额 Δ| 缩放（有大小上下限）。",
      "净值为零不画。",
      "CVD 仅累计已采集事件。",
      "断档后重新预热；旧 K 线不回推，重启后重新采集。");
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
  const api = { COLORS, QUALITY, sizeUnit, radius, readout, details, Primitive };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowLargeTrades = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
