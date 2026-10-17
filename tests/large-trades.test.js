"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const large = require("../static/large-trades.js");

test("大额成交读数区分未采集、预热和真正的零, 详情标明估算、单位及未知方向", () => {
  assert.equal(large.readout(null, 0), "未采集");
  assert.equal(large.readout({ coverage: "warming" }, 0), "基线预热中");
  const s = { coverage: "partial", buy: 40, sell: 30, unknown: 50, delta: 10, cvd: 100,
    count: 3, maxQty: 50, maxRatio: 20, maxZ: null, share: null };
  assert.equal(large.readout(s, 0), "Δ +10 · CVD +100 · 部分");
  const detail = large.details({ time: 1800028800, large: s }, [{ symbol: "SHFE.fu2611",
    source: "snapshot", label: "期货快照估算", minQty: 0, minNotional: 3100000, multiplier: 10,
    windowSec: 1800, warmup: 200, quantile: .99 }]);
  assert.match(detail, /3 次事件 · 最大事件 50 手/);
  assert.match(detail, /买 40 \/ 卖 30 \/ 未知 50 手/);
  assert.match(detail, /≥310 万元 且 >基线 P99\n/);
  assert.doesNotMatch(detail, /均量.*倍/);
  assert.match(detail, /基线：最近 30 分钟交易时间的有效事件，不足 200 个时往前补足/);
  const fallback = large.details({ time: 1800028800, large: s }, [{ symbol: "X", source: "snapshot",
    label: "期货快照估算", minQty: 20, minNotional: 0, windowSec: 1800, warmup: 200, quantile: .99 }]);
  assert.match(fallback, /≥20手 且 >基线 P99/, "没有金额初始值的品种沿用手数门槛");
  assert.match(detail, /最大 Z —/);
  assert.match(detail, /未覆盖整根 K 线/);
  assert.match(detail, /不代表单个订单/);
  assert.match(detail, /未知方向.*不计入 Δ \/ CVD/);
  assert.match(detail, /未知方向.*不画点/);
  assert.match(detail, /买卖各画一个点.*面积按该侧大额量/);
  assert.doesNotMatch(detail, /净值为零/);
});

test("详情标明基线滞后、加密合并口径与缺口补判; 期货不出现加密说明", () => {
  const s = { coverage: "complete", buy: 1, sell: 0, unknown: 0, delta: 1, cvd: 1, count: 1, maxQty: 1, maxRatio: 9, maxZ: 3, share: .1 };
  const base = { minQty: 0, windowSec: 600, warmup: 200, quantile: .99 };
  const crypto = large.details({ time: 1800028800, large: s }, [{ ...base, symbol: "OKX.BTC-USDT-SWAP", source: "trade",
    label: "交易所逐笔", minNotional: 54000, lagSec: 30 }], 4);
  assert.match(crypto, /最近 10 分钟交易时间的有效事件（不含最近 30 秒）/);
  assert.match(crypto, /同一毫秒、同一方向的连续成交合并成一个事件/);
  assert.match(crypto, /缺口回填后补判/);
  const futures = large.details({ time: 1800028800, large: s }, [{ ...base, symbol: "SHFE.fu2611", source: "snapshot",
    label: "期货快照估算", minNotional: 2600000, multiplier: 10, lagSec: 0 }], 0);
  assert.doesNotMatch(futures, /不含最近|同一毫秒|补判/);
  assert.match(futures, /旧 K 线不回推，重启后重新采集/);
});

test("买卖两侧各取各的量: 对冲不抵消, 无事件、无量的一侧不画", () => {
  assert.deepEqual(large.sideVolumes({ count: 2, buy: 80, sell: 80, unknown: 0 }), [["buy", 80], ["sell", 80]]);
  assert.deepEqual(large.sideVolumes({ count: 1, buy: 0, sell: 30 }), [["sell", 30]]);
  assert.deepEqual(large.sideVolumes({ count: 1, buy: 0, sell: 0, unknown: 50 }), [], "只有未知方向时不画点");
  assert.deepEqual(large.sideVolumes({ count: 0, buy: 0, sell: 0 }), []);
  assert.deepEqual(large.sideVolumes(undefined), []);
  assert.deepEqual(large.sideVolumes({ count: 1, buy: null, sell: NaN }), []);
});

test("圆点面积反映该侧大额量, 常见量有明显大小差, 命中跟随重绘且隐藏不残留悬停", () => {
  assert.ok(large.radius(80) > large.radius(20));
  assert.equal(large.radius(160) / large.radius(40), 2, "量四倍时半径两倍, 面积四倍");
  assert.ok(large.radius(489) > 3 * large.radius(40), "本次实盘样本的尺寸差异不能再被压到一两个像素");
  assert.ok(large.radius(1e10) <= 20);
  assert.ok(large.radius(.001) >= 3);
  let marks = [{ x: 10, y: 20, r: 6, side: "buy" }, { x: 50, y: 20, r: 8, side: "sell" },
    { x: -200, y: 20, r: 8, side: "buy" }];
  const p = new large.Primitive(() => marks);
  const colors = [];
  const ctx = { save() {}, restore() {}, beginPath() {}, arc() {}, stroke() {}, fillText() {},
    fill() { colors.push(this.fillStyle); } };
  p.draw({ useMediaCoordinateSpace: (fn) => fn({ context: ctx, mediaSize: { width: 100, height: 60 } }) });
  assert.deepEqual(colors, [large.COLORS.buy + "45", large.COLORS.sell + "45"]);
  assert.equal(p.hit({ x: 12, y: 22 }).side, "buy");
  assert.equal(p.hit({ x: 50, y: 20 }).side, "sell");
  assert.equal(p.hit({ x: 30, y: 20 }), null);
  marks = [{ x: 30, y: 20, r: 6, side: "sell" }];
  assert.equal(p.hit({ x: 30, y: 21 }).side, "sell", "命中跟随当前数据, 不必等下一帧绘制");
  marks = [];
  assert.equal(p.hit({ x: 12, y: 22 }), null, "隐藏或无数据时不残留悬停");
});

test("门槛单位: 金额门槛按参考价格和合约乘数折成手/币, 缺价格或乘数时等待数据", () => {
  assert.equal(large.floorUnit([{ source: "snapshot", minQty: 20, minNotional: 0 }]), 20);
  const futures = [{ source: "snapshot", minQty: 0, minNotional: 3000000, multiplier: 10 }];
  assert.equal(large.floorUnit(futures, 3000), 100, "300 万元 / (3000 元 × 10) = 100 手");
  assert.equal(large.floorUnit([{ ...futures[0], multiplier: null }], 3000), null, "乘数未知不能锁定手数单位");
  const cfg = [{ source: "aggregate", minQty: 0, minNotional: 10000 }];
  assert.equal(large.floorUnit(cfg), null, "没有价格时等待数据, 不能锁定错误的币数量单位");
  const unit = large.floorUnit(cfg, 100000);
  assert.equal(unit, .1);
  assert.equal(large.radius(.4, unit) / large.radius(.1, unit), 2);
  assert.equal(large.floorUnit([{ source: "snapshot", minQty: 0, minNotional: 0 }]), 20);
});

test("尺寸按本图归一: 各侧大额量 P90 画成 16px, 大周期不全部顶到上限, 样本少时只用门槛单位", () => {
  const cfg = [{ source: "snapshot", minQty: 20, minNotional: 0 }];
  // 交替放在买侧、卖侧: 两侧一起作为样本
  const barsOf = (qtys) => qtys.map((q, i) => ({ large: { count: 1, buy: i % 2 ? 0 : q, sell: i % 2 ? q : 0 } }));
  const small = barsOf(Array.from({ length: 19 }, (_, i) => (i + 1) * 1000));
  assert.equal(large.sizeUnit(small, cfg), 20, "不足 20 个样本: 用门槛单位");
  // 1h 级别: 每侧 100~10000 手; 按 20 手单位时绝大多数顶到 20px
  const hourly = barsOf(Array.from({ length: 100 }, (_, i) => (i + 1) * 100));
  const unit = large.sizeUnit(hourly, cfg);
  assert.equal(unit, 9000 / 16);
  assert.equal(large.radius(9000, unit), 16);
  const qtyOf = (b) => b.large.buy + b.large.sell;
  const capped = hourly.filter((b) => large.radius(qtyOf(b), unit) >= 20).length;
  assert.ok(capped <= 5, `只有极少数到上限, 实际 ${capped}`);
  assert.equal(hourly.filter((b) => large.radius(qtyOf(b), 20) >= 20).length, 96, "旧的固定单位几乎全部顶满");
  // 同一根的买卖两侧都计入: 10 根双向 K 线 = 20 个样本
  const both = Array.from({ length: 10 }, () => ({ large: { count: 2, buy: 3200, sell: 1600 } }));
  assert.equal(large.sizeUnit(both, cfg), 3200 / 16);
  // 10s 级别: 多为一两笔门槛大单, P90/16 小于门槛单位, 仍按一笔门槛大单 = 4px
  const quick = barsOf(Array.from({ length: 50 }, (_, i) => 20 + (i % 3) * 20));
  assert.equal(large.sizeUnit(quick, cfg), 20);
  assert.equal(large.sizeUnit([...quick, { large: { count: 0, buy: 0, sell: 0 } }, {}], cfg), 20, "无事件、未采集的 K 线不计入样本");
});

const crypto = [{ source: "aggregate", minQty: 0, minNotional: 10000 }];
const strengthBars = (n, tf = 30) => Array.from({ length: n }, (_, i) => ({
  time: 1800000000 + i * tf, large: { count: 2, buy: i + 1, sell: (i + 1) * 100, coverage: "complete" },
  largeRelative: { count: 2, buy: 1e6, sell: 1e6, coverage: "complete" },
}));

test("亮度只比较过去60分钟同侧已收盘非零大单, 不混入当前根、空心层或未来数据", () => {
  const bars = strengthBars(241);
  bars[120].large.buy = 61;
  bars[120].large.sell = 6100;
  const snapshot = structuredClone(bars);
  const scores = large.strengths(bars, crypto, 30);
  assert.equal(scores[120].buy.samples, 120);
  assert.equal(scores[120].buy.rank, 60.5 / 120, "相同值按中位秩处理");
  assert.equal(scores[120].sell.rank, scores[120].buy.rank, "买卖数量尺度不同仍各自比较");
  assert.deepEqual(bars, snapshot, "不向行情对象写入显示结果");
  bars[121].large.buy = 1e10;
  bars[121].large.sell = 1e10;
  assert.deepEqual(large.strengths(bars, crypto, 30).slice(0, 121), scores.slice(0, 121));
  bars[120].largeRelative.buy = 1e20;
  assert.deepEqual(large.strengthAt(bars, 120, 30), scores[120]);
  bars[120].large.buy = 1e10;
  const changed = large.strengthAt(bars, 120, 30);
  assert.equal(changed.buy.rank, 1);
  assert.equal(changed.buy.samples, scores[120].buy.samples, "当前累计量不能进入自己的参考分布");
  assert.deepEqual(changed.sell, scores[120].sell);
  assert.deepEqual(large.strengths(bars, [{ source: "snapshot" }], 30), large.strengths(bars, crypto, 30), "期货、加密同一套亮度");
  assert.deepEqual(large.strengths(bars, [], 30), []);
});

test("亮度按真实时间窗适配周期, 跳过未收盘、零量和不完整覆盖; 不足20样本保留强调", () => {
  for (const [tf, samples] of [[10, 360], [30, 120], [60, 60], [300, 12], [3600, 1], [14400, 0]]) {
    const score = large.strengthAt(strengthBars(401, tf), 400, tf).buy;
    assert.equal(score.samples, samples);
    assert.equal(score.rank, samples >= 20 ? 1 : null);
    assert(large.markerStyle(score).highlighted);
  }
  const bars = strengthBars(32);
  bars[0].large.buy = 0;
  bars[1].large.coverage = "partial";
  bars[2].large.coverage = "warming";
  bars[3].large.coverage = "missing";
  bars[4].large = null;
  bars[5].large.count = 0;
  bars[6].large.buy = NaN;
  bars[30].time = bars[31].time - 10; // 这一根按30秒周期尚未结束
  const score = large.strengthAt(bars, 31, 30);
  assert.equal(score.buy.samples, 23);
  assert.equal(score.sell.samples, 25);
  assert.equal(large.strengthAt(bars.slice(0, 20), 19, 30).buy.rank, null);
  // 仅19个正量样本, 加入很多零量也不能补足。
  for (let i = 0; i < 12; i++) bars[i].large = { count: 0, buy: 0, sell: 0, coverage: "complete" };
  assert.equal(large.strengthAt(bars, 31, 30).buy.rank, null);
});

test("强弱与预览一致且单调; 弱点仍可见, 两层共用原半径并保留原大单位置", () => {
  assert(!large.markerStyle({ rank: .79999 }).highlighted);
  assert(large.markerStyle({ rank: .8 }).highlighted);
  let previous = 0;
  for (let i = 0; i <= 100; i++) {
    const style = large.markerStyle({ rank: i / 100 });
    assert(style.opacity >= previous && style.opacity > 0);
    previous = style.opacity;
  }
  assert.deepEqual(large.markerStyle({ rank: .8 }), large.markerStyle(null), "突出的与没算亮度的画法相同");
  assert.equal(large.markerStyle({ rank: null, samples: 3 }).opacity, 1, "样本不足保留强调");
  const bars = strengthBars(130), before = structuredClone(bars);
  const unit = large.sizeUnit(bars, crypto, 100);
  const score = large.strengthAt(bars, 129, 30);
  const marks = large.markers(bars[129], unit, score);
  assert.equal(marks.length, 4);
  for (const side of ["buy", "sell"]) {
    const primary = marks.find(m => m.side === side && m.tier === "large");
    const relative = marks.find(m => m.side === side && m.tier === "relative");
    assert.equal(primary.r, large.radius(bars[129].large[side], unit));
    assert.equal(primary.offset, primary.r + 6);
    assert.equal(relative.r, large.radius(bars[129].largeRelative[side], unit));
    assert.equal(relative.offset - primary.offset, primary.r + relative.r + 5);
    assert.equal(relative.style, null, "空心层不参与亮度");
  }
  assert.deepEqual(bars, before);
  const noRelative = bars.map(b => ({ ...b, largeRelative: undefined }));
  assert.equal(large.sizeUnit(noRelative, crypto, 100), unit, "空心层不改变尺寸基准");
});

test("实心只有一种画法(颜色+45 填满、不描边、符号不透明), 亮度只乘整体透明度; 空心只描淡边; 淡化点仍可命中", () => {
  const bar = { large: { count: 2, buy: 40, sell: 80 }, largeRelative: { count: 1, buy: 10, sell: 0 } };
  const marks = large.markers(bar, 20, { buy: { rank: .2, samples: 50 }, sell: { rank: .9, samples: 50 } })
    .map((m, i) => ({ ...m, x: 30 + i * 50, y: 30 }));
  const paint = (list) => {
    const fills = [], strokes = [], symbols = [];
    const ctx = { save() {}, restore() {}, beginPath() {}, arc() {},
      fill() { fills.push([this.fillStyle, this.globalAlpha]); },
      stroke() { strokes.push([this.strokeStyle, this.globalAlpha, this.lineWidth]); },
      fillText(text) { symbols.push([text, this.fillStyle, this.globalAlpha]); } };
    const primitive = new large.Primitive(() => list);
    primitive.draw({ useMediaCoordinateSpace: fn => fn({ context: ctx, mediaSize: { width: 200, height: 100 } }) });
    return { fills, strokes, symbols, primitive };
  };
  const { buy, sell } = large.COLORS, faded = marks[0].style.opacity;
  assert(faded < 1 && marks[2].style.highlighted);
  const { fills, strokes, symbols, primitive } = paint(marks);
  assert.deepEqual(fills, [[buy + "45", faded], [sell + "45", 1]]);
  assert.deepEqual(strokes, [[buy, .42, 1.2]], "只有空心描边");
  assert.deepEqual(symbols, [["+", buy, faded], ["−", sell, 1]]);
  const plain = paint(large.markers(bar, 20).map((m, i) => ({ ...m, x: 30 + i * 50, y: 30 })));
  assert.deepEqual(plain.fills[1], fills[1], "突出的点与没算亮度时画得一样");
  assert.deepEqual(plain.symbols[1], symbols[1]);
  assert.equal(primitive.hit({ x: marks[0].x, y: 30 }), marks[0]);
  const text = large.details({ time: 1800000000, large: { ...bar.large, coverage: "complete" },
    largeRelative: { ...bar.largeRelative, coverage: "complete", delta: 10, cvd: 10 } },
    [{ ...crypto[0], symbol: "BINANCE.XUSDT.P", label: "交易所聚合成交", quantile: .99, windowSec: 600, lagSec: 30, warmup: 200 }], 2,
    { buy: { rank: .2, samples: 50 }, sell: { rank: .9, samples: 50 } });
  assert.match(text, /历史分位 20%（50 个样本）/);
  assert.match(text, /历史分位 90%（50 个样本）/);
  assert.match(text, /两层独立统计/);
  assert.match(text, /淡化点仍计入全部统计/);
});

test("左端回看不满60分钟时按已加载的比; 窗口右移裁短回看后沿用, 回看不变或变长、本根变了才重算", () => {
  const all = strengthBars(200), memo = new Map();
  all.forEach((b, i) => { b.large.buy = (i * 37) % 101 + 1; });
  const at = (scores, from, i) => scores[i - from];
  const loaded = large.strengths(all.slice(40, 190), crypto, 30, memo);
  assert.equal(at(loaded, 40, 100).buy.samples, 60, "只加载到 40 根以前的一半回看");
  assert.equal(at(loaded, 40, 170).buy.samples, 120);
  const slid = large.strengths(all.slice(41, 191), crypto, 30, memo);
  assert.equal(at(large.strengths(all.slice(41, 191), crypto, 30), 41, 100).buy.samples, 59, "不记就会随裁窗变");
  assert.deepEqual(at(slid, 41, 100), at(loaded, 40, 100), "已收盘根不随裁窗变");
  assert(![...memo.keys()].some(t => t < all[41].time), "裁出窗口的不再留着");
  assert(!memo.has(all[190].time), "未收盘的最后一根不记");
  const before = all[130].large.buy;
  all[130].large.buy = .5;   // 修订: 回看完整的根照样按新历史重算, 回看已被裁短的沿用
  const revised = large.strengths(all.slice(41, 191), crypto, 30, memo);
  assert.deepEqual(at(revised, 41, 170), large.strengthAt(all.slice(41, 191), 129, 30));
  assert.notDeepEqual(at(revised, 41, 170), at(slid, 41, 170));
  assert.deepEqual(at(revised, 41, 100), at(loaded, 40, 100));
  all[130].large.buy = before;
  const own = large.strengths(all.slice(42, 192).map((b, i) => i === 58 ? { ...b, large: { ...b.large, buy: 1e9 } } : b),
    crypto, 30, memo);
  assert.equal(at(own, 42, 100).buy.rank, 1, "本根大额量变了要重算");
  const longer = large.strengths(all, crypto, 30, memo);
  assert.equal(longer[100].buy.samples, 100, "补了更早的历史按更长的回看重算");
});

test("价格轴边距只给会画出窗格的点: 极值上的按外沿减 scaleMargins, 离极值远的不占, 比例变小的也算进去", () => {
  const scale = { top: .2, bottom: .1 };
  assert.equal(large.axisMargins([], 10, 300, scale), null);
  assert.equal(large.axisMargins([{ edge: "above", gap: 0, extent: 50 }], 10, 300, scale), null, "上方 60px 的 scaleMargins 放得下");
  assert.deepEqual(large.axisMargins([{ edge: "above", gap: 0, extent: 80 }], 10, 300, scale), { above: 20, below: 0 });
  assert.equal(large.axisMargins([{ edge: "below", gap: 5, extent: 80 }], 10, 300, scale), null, "离最低价半个区间, 远在窗格里");
  // 边距一大比例就变小: 只按留边距前的比例算, 离最高价一点的大点会被切掉
  const items = [{ edge: "above", gap: 0, extent: 50 }, { edge: "above", gap: 1, extent: 60 }];
  const m = large.axisMargins(items, 10, 100);
  assert.equal(m.above, 56);
  assert(m.above + 1 * (100 - m.above) / 10 >= 60 && (m.above - 1) + (100 - m.above + 1) / 10 < 60);
  assert.deepEqual(large.axisMargins(items, 10, 100, {}, { above: 70, below: 3 }), { above: 70, below: 3 }, "成交标记要的更大时用它的");
  // 随机情形: 放得下时每个点外沿都在窗格里, 且每边少 1px 就有点出界; 放不下时不超过最外沿减 scaleMargins
  let seed = 7;
  const rand = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  for (let n = 0; n < 300; n++) {
    const range = 1 + rand() * 50, height = 80 + rand() * 400, sc = { top: rand() * .2, bottom: rand() * .2 };
    const list = Array.from({ length: 1 + Math.floor(rand() * 8) }, () => ({
      edge: rand() < .5 ? "above" : "below", gap: rand() < .3 ? 0 : rand() * range * .9, extent: 10 + rand() * 60 }));
    const got = large.axisMargins(list, range, height, sc) ?? { above: 0, below: 0 };
    const fixed = { above: sc.top * height, below: sc.bottom * height };
    const fits = (mm) => list.every(({ edge, gap, extent }) => fixed[edge] + mm[edge] +
      gap * (height * (1 - sc.top - sc.bottom) - mm.above - mm.below) / range >= extent - 1e-9);
    const cap = { above: 0, below: 0 };
    for (const { edge, extent } of list) cap[edge] = Math.max(cap[edge], Math.ceil(extent - fixed[edge]));
    assert(got.above <= cap.above && got.below <= cap.below);
    if (!fits(cap)) continue;   // 窗格矮到怎么留都放不下
    assert(fits(got), JSON.stringify({ range, height, sc, list, got }));
    for (const edge of ["above", "below"]) if (got[edge] > 0) assert(!fits({ ...got, [edge]: got[edge] - 1 }));
  }
});

test("明细: 两侧都没量不留空行; 期货也写亮度、不写加密说明; 空心层没事件时服务端只给次数、覆盖与 CVD", () => {
  const cfg = [{ ...crypto[0], symbol: "BINANCE.XUSDT.P", label: "交易所聚合成交", quantile: .99, windowSec: 600, lagSec: 30, warmup: 200 }];
  const empty = { count: 0, buy: 0, sell: 0, unknown: 0, delta: 0, cvd: 3, maxQty: 0, maxRatio: 0, maxZ: null, share: 0, coverage: "complete" };
  const bar = { time: 1800000000, large: empty, largeRelative: { count: 0, coverage: "complete", cvd: 5 } };
  const text = large.details(bar, cfg, 2, { buy: { rank: null, samples: 0 }, sell: { rank: null, samples: 0 } });
  assert(!text.split("\n").some((line) => !line.trim()), "不留空行");
  assert.match(text, /相对异常 · Δ 0 · CVD \+5 · 0 次\n买 0 \/ 卖 0 币/);
  assert.equal(large.relativeReadout(bar.largeRelative), "空心 0次");
  assert.equal(large.relativeReadout({ count: 0, coverage: "warming", cvd: null }), "空心 基线预热中");
  assert.deepEqual(large.markers(bar, 20, null), []);
  const mixed = [...cfg, { source: "snapshot", symbol: "SHFE.fu", label: "期货快照估算", quantile: .99, windowSec: 600, lagSec: 30, warmup: 200 }];
  assert.equal(large.cryptoConfigs(mixed), false);
  assert.doesNotMatch(large.details(bar, mixed, 2), /两层独立统计|同一毫秒/, "明细与边距用同一个加密判断");
  const futures = large.details({ time: 1800000000, large: { ...empty, count: 1, buy: 5 } }, mixed.slice(1), 0,
    { buy: { rank: .25, samples: 40 }, sell: { rank: null, samples: 40 } });
  assert.match(futures, /实心亮度/);
  assert.match(futures, /买侧 历史分位 25%（40 个样本）/);
  assert.doesNotMatch(futures, /卖侧|空心|相对异常/, "没量的一侧不列, 期货没有空心层");
});
