/* 原始成交先筛选、再按 K 线汇总的大额事件。只负责展示, 阈值由服务端下发。 */
(function (root) {
  "use strict";
  const COLORS = { buy: "#30cf9b", sell: "#ff6978" };
  const MIN_RADIUS_OPTIONS = [0, 8, 16];   // 0 = 全部, 其余为最小显示半径(px)
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
  // 实心亮度(期货、加密相同)只调显示, 不筛事件。每侧与此前 60 分钟已收盘且覆盖完整的非零同侧量比较。
  // 不混入当前根、另一侧、空心层或未来数据; 缺失/预热/部分覆盖不能充当低量样本。
  // 60 分钟按时间算: 期货午休、收盘后开盘那阵回看样本少, 不足 STRENGTH_SAMPLE 个时保留强调。
  const STRENGTH_WINDOW_SEC = 3600, STRENGTH_SAMPLE = 20, HIGHLIGHT_RANK = .8;
  const cryptoConfigs = (configs = []) => configs.length > 0 && configs.every((c) => ["aggregate", "trade"].includes(c.source));
  const roundedQty = (v) => finite(v) && v > 0 ? Number(v.toFixed(8)) : 0;
  function strengthAt(bars, index, tf) {
    const current = bars[index];
    const sides = SIDES.map((side) => ({ side, qty: roundedQty(current?.large?.[side]), samples: 0, less: 0, equal: 0 }));
    if (finite(current?.time) && finite(tf) && tf > 0) {
      const cutoff = current.time - STRENGTH_WINDOW_SEC;
      for (let i = index - 1; i >= 0; i--) {   // 两侧一起扫一遍回看
        const b = bars[i];
        if (b.time < cutoff) break;
        if (!finite(b.time) || b.time + tf > current.time || b.large?.coverage !== "complete" || !b.large.count) continue;
        for (const c of sides) {
          const value = roundedQty(b.large[c.side]);
          if (!value) continue;
          c.samples++;
          if (value < c.qty) c.less++;
          else if (value === c.qty) c.equal++;
        }
      }
    }
    return Object.fromEntries(sides.map(({ side, qty, samples, less, equal }) =>
      [side, { rank: qty && samples >= STRENGTH_SAMPLE ? (less + equal * .5) / samples : null, samples }]));
  }
  // 页面最左端的根往前不满 60 分钟时只能和已加载的部分比。已收盘根的结果按 K 线时间记在 memo 里:
  // 窗口右移把回看裁短后沿用记下的, 不随裁窗来回变; 回看与上次一样长或更长(补了历史)、本根大额量变了才重算。
  function strengths(bars = [], configs = [], tf, memo = null) {
    if (!configs.length) return [];   // 本合约没有大额检测
    const first = bars[0]?.time;
    const result = bars.map((bar, i) => {
      const from = Math.max(first, bar.time - STRENGTH_WINDOW_SEC), own = `${bar.large?.buy}/${bar.large?.sell}`;
      const kept = memo?.get(bar.time);
      if (kept && kept.from < from && kept.own === own) return kept.value;
      const value = strengthAt(bars, i, tf);
      if (memo && i < bars.length - 1) memo.set(bar.time, { from, own, value });
      return value;
    });
    if (memo) for (const time of memo.keys()) if (!(time >= first)) memo.delete(time);
    return result;
  }
  // 实心画法只有一种(颜色 + 45 填满、不描边、符号不透明), 亮度只乘整体透明度: 突出的(前 20%、样本不足)
  // 和还没算亮度的照原样画, 其余整体变淡, 越弱越淡。
  function markerStyle(strength) {
    if (!strength || strength.rank == null || strength.rank >= HIGHLIGHT_RANK) return { opacity: 1, highlighted: true };
    const level = Math.min(1, Math.max(0, strength.rank / HIGHLIGHT_RANK)) ** 2;
    return { opacity: .18 + .22 * level, highlighted: false };
  }
  // 空心层(加密相对异常)套在同侧实心外面, 不参与亮度。
  function markers(bar, unit, strength, minRadius = 0) {
    const result = [], large = new Map(sideVolumes(bar?.large)), relative = new Map(sideVolumes(bar?.largeRelative));
    for (const side of SIDES) {
      let rLarge = large.has(side) ? radius(large.get(side), unit) : 0;
      if (rLarge < minRadius) rLarge = 0;   // 隐藏的实心点不占空心点内侧的空间
      for (const [tier, volumes] of [["large", large], ["relative", relative]]) {
        if (!volumes.has(side)) continue;
        const qty = volumes.get(side), r = radius(qty, unit), score = tier === "large" ? strength?.[side] : null;
        if (r < minRadius) continue;   // 只筛显示, 尺寸基准和成交统计仍使用全部数据
        result.push({ side, tier, qty, r, strength: score,
          offset: tier === "large" ? r + 6 : rLarge ? rLarge * 2 + 11 + r : r + 6,
          style: tier === "large" ? markerStyle(score) : null });
      }
    }
    return result;
  }
  // 价格轴要给圆点留的像素边距: 只有按当前比例会画出窗格的点才要, 离可见区最高/最低价远的不占。
  // items: [{ edge: "above"|"below", gap: 所在 K 线高(低)点离可见区极值的价差, extent: 圆点外沿离该点的像素 }]。
  // 库(5.2.1)把价格区间线性铺进 内高 = 高度 − 上下 scaleMargins − 上下边距, scaleMargins 那段本身也放得下圆点;
  // 点离极值 gap / range × 内高 像素, 边距越大它越近。按 extent ≤ scaleMargins + 边距 + 离极值像素 直接解出每个点要的边距,
  // 两边互相影响, 轮流解到不再变。floor 是别的图元(成交标记)要的边距, 取大。每边以最外沿减 scaleMargins 封顶:
  // 比例再小这么多也够; 能放下时解不会碰到它, 窗格矮到放不下时也不越解越大。
  function axisMargins(items, range, height, scale = {}, floor = null) {
    if (!items.length || !(height > 0)) return null;
    const fixed = { above: (scale.top ?? 0) * height, below: (scale.bottom ?? 0) * height };
    const inner = height - fixed.above - fixed.below;
    const cap = { above: floor?.above ?? 0, below: floor?.below ?? 0 };
    for (const { edge, extent } of items) cap[edge] = Math.max(cap[edge], Math.ceil(extent - fixed[edge]));
    const solve = (edge, other) => {
      let margin = floor?.[edge] ?? 0;
      for (const { edge: e, gap, extent } of items) {
        const share = e === edge ? (range > 0 ? gap / range : 0) : 1;
        if (share < 1) margin = Math.max(margin, Math.ceil((extent - fixed[edge] - share * (inner - other)) / (1 - share)));
      }
      return Math.min(margin, cap[edge]);
    };
    let margins = { above: 0, below: 0 };
    for (let n = 0; n < 20; n++) {   // 只增不减, 一般两三轮就停
      const above = solve("above", margins.below), below = solve("below", above);
      if (above === margins.above && below === margins.below) break;
      margins = { above, below };
    }
    return margins.above || margins.below ? margins : null;
  }
  // 未采集/预热时没有可信读数, 只报覆盖状态
  const pending = (s) => !s || ["missing", "warming"].includes(s.coverage);
  const coverageText = (s) => QUALITY[s?.coverage] || QUALITY.missing;
  function readout(s, digits) {
    if (pending(s)) return coverageText(s);
    return `Δ ${signed(s.delta, digits)}` +
      ` · CVD ${signed(s.cvd, digits)}${s.coverage === "partial" ? " · 部分" : ""}`;
  }
  // 图例里空心层只报次数
  function relativeReadout(s) {
    return `空心 ${pending(s) ? coverageText(s) : number(s.count, 0) + "次"}`;
  }
  // 空心层没有事件的根服务端只发 { count: 0, coverage, cvd }(省掉一串零), 读明细前补齐
  const EMPTY = { buy: 0, sell: 0, unknown: 0, count: 0, maxQty: 0, maxRatio: 0, maxZ: null, delta: 0, share: null };
  const relativeOf = (bar) => bar?.largeRelative && { ...EMPTY, ...bar.largeRelative };
  function details(bar, configs = [], digits = 0, strength = null, tier = "large") {
    const relative = tier === "relative", s = relative ? relativeOf(bar) : bar?.large;
    const label = relative ? "相对异常（空心）" : "大额成交";
    if (!s) return `${label} · 未采集\n历史 K 线不能反推出原始大额事件。`;
    const crypto = cryptoConfigs(configs);
    const unit = crypto ? "币" : "手";
    const time = new Date(bar.time * 1000).toISOString().slice(5, 19).replace("T", " ");
    const lines = [`${label} · ${time} · ${coverageText(s)}`];
    if (!pending(s)) {
      lines.push(`买 ${number(s.buy, digits)} / 卖 ${number(s.sell, digits)} / 未知 ${number(s.unknown, digits)} ${unit}`,
        `${relative ? "异常" : "大额"} Δ ${signed(s.delta, digits)} · 本次采集 CVD ${signed(s.cvd, digits)}`,
        `${number(s.count, 0)} 次事件 · 最大事件 ${number(s.maxQty, digits)} ${unit}`,
        `最大相对量 ${s.count ? number(s.maxRatio, 2) + "×" : "—"} · 最大 Z ${number(s.maxZ, 2)}`,
        `${relative ? "异常" : "大额"}量占比 ${finite(s.share) ? number(s.share * 100, 1) + "%" : s.coverage === "complete"
          ? "—（总量为零或待核对）" : "—（未覆盖整根 K 线）"}`);
    }
    for (const c of configs) {
      const floor = [c.minNotional > 0 ? `${relative ? "<" : "≥"}${money(c.minNotional, c.source)}` : "",
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
    if (crypto) lines.push("实心为原大单，空心为超过 P99、低于金额门槛的相对异常；两层独立统计。");
    if (!relative) {
      lines.push("实心亮度：与过去 60 分钟已收盘、覆盖完整的同侧非零累计量比较，历史前 20% 照常显示，其余渐淡；不足 20 个样本时保留强调。淡化点仍计入全部统计。");
      const ranks = strength ? SIDES.filter((side) => s[side] > 0).map((side) => {
        const score = strength[side];
        return `${side === "buy" ? "买" : "卖"}侧 ${score?.rank == null ? "样本不足" : `历史分位 ${number(score.rank * 100, 1)}%`}（${score?.samples ?? 0} 个样本）`;
      }) : [];
      if (ranks.length) lines.push(ranks.join(" · "));   // 两侧都没量时不留空行
    }
    if (crypto) {
      const other = relative ? bar.large : relativeOf(bar);
      if (other) lines.push(`${relative ? "原大单" : "相对异常"} · ${readout(other, digits)} · ${number(other.count, 0)} 次`,
        `买 ${number(other.buy, digits)} / 卖 ${number(other.sell, digits)} ${unit}`);
    }
    return lines.join("\n");
  }

  // 命中按当前几何现算, 不依赖上一帧画了什么: 数据更新后立即重查悬停, 不必挂在绘制回调里。
  class Primitive {
    constructor(geometry, margins = null) {
      this.geometry = geometry;
      this.margins = margins;
      this._view = { zOrder: () => "top", renderer: () => ({ draw: (target) => this.draw(target) }) };
    }
    attached({ requestUpdate }) { this.requestUpdate = requestUpdate; }
    detached() { this.requestUpdate = null; }
    paneViews() { return [this._view]; }
    autoscaleInfo(start, end) {
      const margins = this.margins?.(start, end);
      return margins ? { priceRange: null, margins } : null;
    }
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
          if (m.tier === "relative") {   // 空心: 只描淡边, 不填充、不写符号
            ctx.globalAlpha = .42;
            ctx.strokeStyle = COLORS[m.side];
            ctx.lineWidth = 1.2;
            ctx.stroke();
            continue;
          }
          ctx.globalAlpha = m.style?.opacity ?? 1;   // 淡化的点填充与符号一起变淡
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
  const api = { COLORS, QUALITY, MIN_RADIUS_OPTIONS, sideVolumes, floorUnit, sizeUnit, radius, cryptoConfigs, strengthAt, strengths,
    markerStyle, markers, axisMargins, readout, relativeReadout, details, Primitive };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowLargeTrades = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
