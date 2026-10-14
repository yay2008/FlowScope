"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { markers, SessionEndPrimitive } = require("../static/session-markers.js");

// 图表把北京时间当 UTC 展示; 测试与电脑所在时区无关。
const at = (clock, date = "2026-10-13") => Date.parse(`${date}T${clock}Z`) / 1000;
const barsAt = (...clocks) => clocks.map((clock) => ({ time: at(clock) }));
const day = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]];
const schedule = { day, night: [["21:00:00", "25:00:00"]] };

test("日盘每节截止都落在末根右侧, 复市后的第一根不被当成截止", () => {
  for (const tf of [10, 30]) {
    const bars = [at("10:15:00") - tf, at("10:30:00"), at("11:30:00") - tf,
                  at("13:30:00"), at("15:00:00") - tf].map((time) => ({ time }));
    const result = markers(bars, tf, schedule);
    assert.deepEqual(result.map((m) => m.time), [at("10:15:00"), at("11:30:00"), at("15:00:00")]);
    assert.deepEqual(result.map((m) => m.offset), [0.5, 0.5, 0.5]);
    assert.deepEqual(result.map((m) => m.barTime), [bars[0].time, bars[2].time, bars[4].time]);
  }
});

test("各品种夜盘结束按元数据, 23:00、25:00、26:30 正确显示且跨午夜不重复", () => {
  for (const [end, lastClock, label] of [
    ["23:00:00", "22:59:30", "23:00 截止"],
    ["25:00:00", "00:59:30", "01:00 截止"],
    ["26:30:00", "02:29:30", "02:30 截止"],
  ]) {
    const lastDate = end === "23:00:00" ? "2026-10-16" : "2026-10-17";
    const bars = [{ time: at("21:00:00", "2026-10-16") }, { time: at(lastClock, lastDate) }];
    const result = markers(bars, 30, { day, night: [["21:00:00", end]] });
    assert.equal(result.length, 1, "周五夜盘到周六凌晨属于同一时段");
    assert.equal(result[0].label, label);
    assert.equal(result[0].time, bars[1].time + 30);
    assert.equal(result[0].offset, 0.5);
  }
  assert.equal(markers(barsAt("00:59:30"), 30, schedule)[0].time, at("01:00:00"));
});

test("夜盘在 00:00 结束及常规跨日写法也能定位", () => {
  const bars = barsAt("23:59:30");
  for (const end of ["24:00:00", "00:00:00"]) {
    const [result] = markers(bars, 30, { night: [["21:00:00", end]] });
    assert.equal(result.time, at("00:00:00", "2026-10-14"));
    assert.equal(result.label, "00:00 截止");
  }
});

test("当前时段未收尾时可向右投影, 到下一根数据时截止位置保持一致", () => {
  const first = markers(barsAt("10:14:00"), 30, schedule)[0];
  const next = markers(barsAt("10:14:00", "10:14:30"), 30, schedule)[0];
  assert.equal(first.offset, 1.5);
  assert.equal(next.offset, 0.5);
  assert.equal(first.offset, 1 + next.offset);
});

test("缺行情的整节、周末与节假日不靠日历补标记, 未知时段不猜", () => {
  const bars = [{ time: at("14:59:30", "2026-10-09") }, { time: at("09:00:00") }];
  assert.deepEqual(markers(bars, 30, schedule).map((m) => m.time),
    [at("15:00:00", "2026-10-09"), at("10:15:00")]);
  assert.deepEqual(markers(barsAt("10:30:00"), 30, schedule).map((m) => m.time), [at("11:30:00")]);
  for (const value of [null, {}, { day: [], night: [] }, { day: "bad" },
    { day: [["bad", "10:15:00"], ["09:00:00", "09:00:00"], ["49:00:00", "50:00:00"]] }]) {
    assert.deepEqual(markers(bars, 30, value), []);
  }
  assert.deepEqual(markers([], 30, schedule), []);
});

test("大周期跨几个小节仍包含每个截止, 同一根内按交易时长排列", () => {
  const result = markers(barsAt("09:00:00"), 14400, schedule);
  assert.deepEqual(result.map((m) => m.label), ["10:15 截止", "11:30 截止", "15:00 截止"]);
  assert.ok(result[0].offset < result[1].offset && result[1].offset < result[2].offset);
  assert.equal(result[2].offset, 0.5);
  // 1h 最后一根只剩半小时, 也应在本根右侧收尾。
  assert.equal(markers(barsAt("14:30:00"), 3600, schedule)[0].offset, 0.5);
});

test("绘制跟随真实时间坐标和缩放, 超出视口不画, 密集标签避让, 不改变行情", () => {
  let shift = 0, spacing = 20;
  const primitive = new SessionEndPrimitive(() => ({
    options: () => ({ barSpacing: spacing }), timeToCoordinate: (t) => t + shift,
  }));
  const data = [-60, 30, 40, 150, 290].map((barTime) => ({ barTime, offset: 0.5, label: "10:15 截止" }));
  let updates = 0;
  primitive.attached({ requestUpdate: () => updates++ });
  primitive.setMarkers(data);
  assert.equal(updates, 1);
  const lines = [], labels = [];
  const ctx = {
    save() {}, restore() {}, setLineDash() {}, beginPath() {}, stroke() {}, lineTo() {}, fillRect() {},
    moveTo: (x) => lines.push(x), measureText: () => ({ width: 64 }), fillText: (s, x) => labels.push({ s, x }),
  };
  const draw = () => primitive.paneViews()[0].renderer().draw({
    useMediaCoordinateSpace: (fn) => fn({ context: ctx, mediaSize: { width: 250, height: 200 } }),
  });
  draw();
  assert.deepEqual(lines, [40, 50, 160]);
  assert.equal(labels.length, 2, "相邻两条截止线的标签不重叠");
  lines.length = labels.length = 0;
  shift = 10; spacing = 40;
  draw();
  assert.deepEqual(lines, [60, 70, 180]);
  assert.equal(data.length, 5);
  primitive.setMarkers([]);
  lines.length = 0;
  draw();
  assert.deepEqual(lines, []);
});
