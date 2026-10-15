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
    source: "snapshot", label: "期货快照估算", minQty: 20, minNotional: 0,
    window: 2000, warmup: 200, quantile: .99, minRatio: 1.5 }]);
  assert.match(detail, /3 次事件 · 最大事件 50 手/);
  assert.match(detail, /买 40 \/ 卖 30 \/ 未知 50 手/);
  assert.match(detail, /≥20手 且 >P99 且 ≥均量1.5倍/);
  assert.match(detail, /最大 Z —/);
  assert.match(detail, /未覆盖整根 K 线/);
  assert.match(detail, /不代表单个订单/);
  assert.match(detail, /未知方向.*不计入 Δ \/ CVD/);
  assert.match(detail, /每根 K 线一个净方向点.*面积按/);
  assert.match(detail, /净值为零不画/);
});

test("圆点面积反映净量, 常见净量有明显大小差, 命中跟随重绘且隐藏不残留悬停", () => {
  assert.ok(large.radius(80) > large.radius(20));
  assert.equal(large.radius(160) / large.radius(40), 2, "净量四倍时半径两倍, 面积四倍");
  assert.ok(large.radius(489) > 3 * large.radius(40), "本次实盘样本的尺寸差异不能再被压到一两个像素");
  assert.equal(large.radius(-40), large.radius(40), "相同净量的买卖方向应同样大");
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

test("期货用手数门槛作尺寸基准, 加密小数币数量按同一参考价格换算后能区分大小", () => {
  assert.equal(large.sizeUnit([{ source: "snapshot", minQty: 20, minNotional: 0 }]), 20);
  const cfg = [{ source: "aggregate", minQty: 0, minNotional: 10000 }];
  assert.equal(large.sizeUnit(cfg), null, "没有价格时等待数据, 不能锁定错误的币数量单位");
  const unit = large.sizeUnit(cfg, 100000);
  assert.equal(unit, .1);
  assert.equal(large.radius(.4, unit) / large.radius(.1, unit), 2);
  assert.equal(large.sizeUnit([{ source: "snapshot", minQty: 0, minNotional: 0 }]), 20);
});
