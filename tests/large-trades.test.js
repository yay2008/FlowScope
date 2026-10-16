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
