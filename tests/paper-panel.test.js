"use strict";
/* 模拟交易面板的纯函数: 成交标记对齐到 bar、价格线、金额格式。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { snapTime, buildMarkers, priceLines, formatMoney, formatSigned, pnlClass, orderLabel,
        BUY_COLOR, SELL_COLOR } = require("../static/paper-panel.js");

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

test("成交标记: 买在下方红色上箭头, 卖在上方绿色下箭头, 按时间升序", () => {
  const markers = buildMarkers([
    { side: "sell", qty: 2, time: 1065 },
    { side: "buy", qty: 1, time: 1001 },
    { side: "buy", qty: 3, time: 500 },
  ], bars);
  assert.deepEqual(markers, [
    { time: 1000, position: "belowBar", shape: "arrowUp", color: BUY_COLOR, text: "买1" },
    { time: 1060, position: "aboveBar", shape: "arrowDown", color: SELL_COLOR, text: "卖2" },
  ]);
  assert.deepEqual(buildMarkers(undefined, bars), []);
});

test("价格线只画当前合约的持仓均价与挂单", () => {
  const state = {
    contract: "SHFE.fu2611",
    positions: [{ contract: "SHFE.fu2611", qty: -2, avgPrice: 3000 },
                { contract: "SHFE.fu2609", qty: 1, avgPrice: 2900 }],
    orders: [{ contract: "SHFE.fu2611", status: "open", side: "buy", qty: 2, price: 2980 },
             { contract: "SHFE.fu2611", status: "filled", side: "sell", qty: 1, price: 3010 },
             { contract: "SHFE.fu2609", status: "open", side: "sell", qty: 1, price: 2950 }],
  };
  assert.deepEqual(priceLines(state), [
    { price: 3000, color: SELL_COLOR, style: "solid", title: "空2 均价" },
    { price: 2980, color: BUY_COLOR, style: "dashed", title: "买2 挂单" },
  ]);
  assert.deepEqual(priceLines({ contract: null }), []);
  assert.deepEqual(priceLines(null), []);
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
});
