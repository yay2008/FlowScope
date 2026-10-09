"use strict";
/* 模拟交易面板的纯函数: 成交标记对齐到 bar、价格线、金额格式。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { snapTime, buildMarkers, priceLines, dragTargets, dragLine, snapPrice, formatMoney, formatSigned, pnlClass,
        orderLabel, stopsText, BUY_COLOR, SELL_COLOR, TP_COLOR, SL_COLOR } = require("../static/paper-panel.js");

const bars = [{ time: 1000 }, { time: 1030 }, { time: 1060 }, { time: 1200 }];

test("成交时间对齐到所在 bar; 早于第一根的不画, 休市缺口里的挂到前一根", () => {
  assert.equal(snapTime(bars, 999), null);
  assert.equal(snapTime(bars, 1000), 1000);
  assert.equal(snapTime(bars, 1029), 1000);
  assert.equal(snapTime(bars, 1030), 1030);
  assert.equal(snapTime(bars, 1100), 1060);   // 缺口
  assert.equal(snapTime(bars, 5000), 1200);
  assert.equal(snapTime([], 1000), null);
});

test("成交标记: 买在下方红色 B, 卖在上方绿色 S, 不画箭头, 按时间升序", () => {
  const markers = buildMarkers([
    { side: "sell", qty: 2, time: 1065 },
    { side: "buy", qty: 1, time: 1001 },
    { side: "buy", qty: 3, time: 500 },
  ], bars);
  assert.deepEqual(markers, [
    { time: 1000, position: "belowBar", shape: "circle", size: 0, color: BUY_COLOR, text: "B" },
    { time: 1060, position: "aboveBar", shape: "circle", size: 0, color: SELL_COLOR, text: "S" },
  ]);
  assert.deepEqual(buildMarkers(undefined, bars), []);
});

test("价格线只画当前合约的持仓均价、止盈止损与挂单", () => {
  const state = {
    contract: "SHFE.fu2611",
    positions: [{ contract: "SHFE.fu2611", qty: -2, avgPrice: 3000, tp: 2900, sl: 3050 },
                { contract: "SHFE.fu2609", qty: 1, avgPrice: 2900, tp: 3000, sl: 2800 }],
    orders: [{ contract: "SHFE.fu2611", status: "open", side: "buy", qty: 2, price: 2980 },
             { contract: "SHFE.fu2611", status: "filled", side: "sell", qty: 1, price: 3010 },
             { contract: "SHFE.fu2609", status: "open", side: "sell", qty: 1, price: 2950 }],
  };
  assert.deepEqual(priceLines(state), [
    { kind: "avg", price: 3000, color: SELL_COLOR, style: "solid", title: "空2 均价" },
    { kind: "tp", price: 2900, color: TP_COLOR, style: "dotted", title: "止盈" },
    { kind: "sl", price: 3050, color: SL_COLOR, style: "dotted", title: "止损" },
    { kind: "order", price: 2980, color: BUY_COLOR, style: "dashed", title: "买2 挂单" },
  ]);
  assert.deepEqual(priceLines({ contract: null }), []);
  assert.deepEqual(priceLines(null), []);
});

test("划线: 能拖的是止盈、止损和均价线(止盈止损在前); 没有持仓就没有", () => {
  const state = { contract: "SHFE.fu2611",
                  positions: [{ contract: "SHFE.fu2611", qty: 2, avgPrice: 3000, sl: 2950 }] };
  assert.deepEqual(dragTargets(state), [{ from: "sl", price: 2950 }, { from: "avg", price: 3000 }]);
  assert.deepEqual(dragTargets({ contract: "SHFE.fu2611", positions: [] }), []);
  assert.deepEqual(dragTargets(null), []);
});

test("划线: 价位对齐最小变动价位, 从均价线拖出按落在最新价哪一侧定止盈止损, 标题带预估盈亏", () => {
  assert.equal(snapPrice(3012.4, 1, 0), 3012);
  assert.equal(snapPrice(84000.26, 0.1, 1), 84000.3);
  assert.equal(snapPrice(5.55, 0, 0), 5.55);
  const long = { contract: "SHFE.fu2611", quote: { last: 3010, priceTick: 1, priceDecs: 0 },
                 positions: [{ contract: "SHFE.fu2611", qty: 2, avgPrice: 3000, multiplier: 10, tp: 3100 }] };
  assert.deepEqual(dragLine(long, "avg", 3040.3), { kind: "tp", price: 3040, color: TP_COLOR, title: "止盈 +800" });
  assert.deepEqual(dragLine(long, "avg", 3005), { kind: "sl", price: 3005, color: SL_COLOR, title: "止损 +100" });
  // 拖原来的线不换种类(拖过了最新价由服务端拒)
  assert.equal(dragLine(long, "tp", 2990).kind, "tp");
  const short = { mode: "crypto", contract: "BINANCE.BTCUSDT.P", quote: { last: 100, priceTick: 0.1, priceDecs: 1 },
                  positions: [{ contract: "BINANCE.BTCUSDT.P", qty: -0.5, avgPrice: 101 }] };
  assert.deepEqual(dragLine(short, "avg", 95.04), { kind: "tp", price: 95, color: TP_COLOR, title: "止盈 +3.00" });
  assert.equal(dragLine(short, "avg", 104).title, "止损 -1.50");
  assert.equal(dragLine(short, "avg", null), null);
  assert.equal(dragLine({ contract: "x", positions: [] }, "avg", 1), null);
});

test("金额与盈亏格式", () => {
  assert.equal(formatMoney(1000200.4), "1,000,200");
  assert.equal(formatMoney(0.4299, 2), "0.43");
  assert.equal(formatMoney(null), "—");
  assert.equal(formatMoney(NaN), "—");
  assert.equal(formatSigned(40), "+40");
  assert.equal(formatSigned(-20), "-20");
  assert.equal(formatSigned(0), "0");
  assert.equal(pnlClass(1), "up");
  assert.equal(pnlClass(-1), "down");
  assert.equal(pnlClass(0), "");
  assert.equal(orderLabel({ side: "buy", qty: 2, type: "limit", price: 2980 }, 0), "买 2 限 2980");
  assert.equal(orderLabel({ side: "sell", qty: 1, type: "market", price: null }, 0), "卖 1 市价");
  assert.equal(orderLabel({ side: "sell", qty: 1, type: "market", price: null, reason: "止损" }, 0), "卖 1 止损");
  assert.equal(orderLabel({ side: "buy", qty: 1, type: "market", price: null, reason: "已撤单" }, 0), "买 1 市价");
});

test("委托带的止盈止损: 只列填了的, 都没填是空串", () => {
  assert.equal(stopsText({ tp: 3100, sl: 2950 }, 0), "止盈 3100 · 止损 2950");
  assert.equal(stopsText({ tp: null, sl: 84000.5 }, 1), "止损 84000.5");
  assert.equal(stopsText({}, 0), "");
});
