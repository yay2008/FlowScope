"use strict";
/* 模拟交易面板的纯函数: 成交标记对齐到 bar、价格线、金额格式。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { snapTime, buildMarkers, priceLines, lineKey, dragTargets, dragLine, amendProblem, menuChoices, snapPrice,
        formatMoney, formatSigned, pnlClass, orderLabel, stopsText,
        BUY_COLOR, SELL_COLOR, TP_COLOR, SL_COLOR } = require("../static/paper-panel.js");

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

test("价格线只画当前合约的持仓均价、止盈止损与挂单(限价虚线、条件长虚线)", () => {
  const state = {
    contract: "SHFE.fu2611",
    positions: [{ contract: "SHFE.fu2611", qty: -2, avgPrice: 3000, tp: 2900, sl: 3050 },
                { contract: "SHFE.fu2609", qty: 1, avgPrice: 2900, tp: 3000, sl: 2800 }],
    orders: [{ id: "O5", contract: "SHFE.fu2611", status: "open", type: "limit", side: "buy", qty: 2, price: 2980 },
             { id: "O6", contract: "SHFE.fu2611", status: "open", type: "stop", side: "sell", qty: 1, price: 2960 },
             { id: "O4", contract: "SHFE.fu2611", status: "filled", side: "sell", qty: 1, price: 3010 },
             { id: "O3", contract: "SHFE.fu2609", status: "open", side: "sell", qty: 1, price: 2950 }],
  };
  const lines = priceLines(state);
  assert.deepEqual(lines, [
    { kind: "avg", price: 3000, color: SELL_COLOR, style: "solid", title: "空2 均价" },
    { kind: "tp", price: 2900, color: TP_COLOR, style: "dotted", title: "止盈" },
    { kind: "sl", price: 3050, color: SL_COLOR, style: "dotted", title: "止损" },
    { kind: "order", id: "O5", price: 2980, color: BUY_COLOR, style: "dashed", title: "买2 限价" },
    { kind: "order", id: "O6", price: 2960, color: SELL_COLOR, style: "largeDashed", title: "卖1 条件" },
  ]);
  assert.deepEqual(lines.map(lineKey), ["avg", "tp", "sl", "order:O5", "order:O6"]);
  assert.deepEqual(priceLines({ contract: null }), []);
  assert.deepEqual(priceLines(null), []);
});

test("划线: 能拖的是止盈、止损、当前合约的挂单和均价线(均价线最后); 没有持仓也能拖挂单", () => {
  const orders = [{ id: "O7", contract: "SHFE.fu2611", status: "open", type: "limit", side: "buy", qty: 1, price: 2990 },
                  { id: "O8", contract: "SHFE.fu2609", status: "open", type: "limit", side: "buy", qty: 1, price: 2900 }];
  const state = { contract: "SHFE.fu2611", orders,
                  positions: [{ contract: "SHFE.fu2611", qty: 2, avgPrice: 3000, sl: 2950 }] };
  assert.deepEqual(dragTargets(state), [{ from: "sl", price: 2950 }, { from: "order:O7", price: 2990 },
                                        { from: "avg", price: 3000 }]);
  assert.deepEqual(dragTargets({ contract: "SHFE.fu2611", positions: [], orders }), [{ from: "order:O7", price: 2990 }]);
  assert.deepEqual(dragTargets({ contract: "SHFE.fu2611", positions: [] }), []);
  assert.deepEqual(dragTargets(null), []);
});

test("拖挂单改价: 会当场成交的限价、没在突破一侧的触发价标出原因(和服务端校验一致)", () => {
  const quote = { last: 3000, ask: 3001, bid: 2999, priceTick: 1, priceDecs: 0 };
  const buy = { id: "O1", contract: "c", status: "open", type: "limit", side: "buy", qty: 1, price: 2990 };
  const sell = { ...buy, id: "O2", side: "sell", price: 3010 };
  const stopBuy = { ...buy, id: "O3", type: "stop", price: 3010 };
  const stopSell = { ...buy, id: "O4", type: "stop", side: "sell", price: 2990 };
  const state = { contract: "c", quote, positions: [], orders: [buy, sell, stopBuy, stopSell] };
  assert.equal(amendProblem(state, buy, 3000), "");
  assert.equal(amendProblem(state, buy, 3001), "会立即成交");          // 够到卖一
  assert.equal(amendProblem({ ...state, quote: { ...quote, last: 2995 } }, buy, 2996), "会立即成交");   // 高于最新价
  assert.equal(amendProblem(state, sell, 3000), "");
  assert.equal(amendProblem(state, sell, 2999), "会立即成交");
  assert.equal(amendProblem(state, stopBuy, 3001), "");
  assert.equal(amendProblem(state, stopBuy, 3000), "触发价要高于最新价");
  assert.equal(amendProblem(state, stopSell, 3000), "触发价要低于最新价");
  assert.equal(amendProblem({ ...state, quote: {} }, stopSell, 3100), "");     // 没有最新价交给服务端判
  assert.deepEqual(dragLine(state, "order:O1", 2995.4),
                   { kind: "order", id: "O1", price: 2995, problem: "", color: BUY_COLOR, title: "买1 限价" });
  assert.deepEqual(dragLine(state, "order:O3", 2999.6),
                   { kind: "order", id: "O3", price: 3000, problem: "触发价要高于最新价", color: BUY_COLOR,
                     title: "买1 条件 · 触发价要高于最新价" });
  assert.equal(dragLine(state, "order:O9", 3000), null);
});

test("右键挂单: 最新价下方买限价/卖条件, 上方卖限价/买条件, 正好在最新价上给两个限价", () => {
  assert.deepEqual(menuChoices(3000, 2990), [{ side: "buy", type: "limit" }, { side: "sell", type: "stop" }]);
  assert.deepEqual(menuChoices(3000, 3010), [{ side: "sell", type: "limit" }, { side: "buy", type: "stop" }]);
  assert.deepEqual(menuChoices(3000, 3000), [{ side: "buy", type: "limit" }, { side: "sell", type: "limit" }]);
  assert.deepEqual(menuChoices(null, 3000), [{ side: "buy", type: "limit" }, { side: "sell", type: "limit" }]);
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
  assert.equal(orderLabel({ side: "buy", qty: 1, type: "stop", price: 3010 }, 0), "买 1 条件 ≥3010");
  assert.equal(orderLabel({ side: "sell", qty: 1, type: "stop", price: 84000.5 }, 1), "卖 1 条件 ≤84000.5");
  assert.equal(orderLabel({ side: "sell", qty: 1, type: "market", price: null }, 0), "卖 1 市价");
  assert.equal(orderLabel({ side: "sell", qty: 1, type: "market", price: null, reason: "止损" }, 0), "卖 1 止损");
  assert.equal(orderLabel({ side: "buy", qty: 1, type: "market", price: null, reason: "已撤单" }, 0), "买 1 市价");
});

test("委托带的止盈止损: 只列填了的, 都没填是空串", () => {
  assert.equal(stopsText({ tp: 3100, sl: 2950 }, 0), "止盈 3100 · 止损 2950");
  assert.equal(stopsText({ tp: null, sl: 84000.5 }, 1), "止损 84000.5");
  assert.equal(stopsText({}, 0), "");
  assert.equal(stopsText({ autoStopLoss: 100, status: "open" }, 0), "自动止损 100.00 元 · 止盈 150.00 元");
  assert.equal(stopsText({ autoStopLoss: 200, status: "filled", autoStopApplied: true }, 0), "自动止损 200.00 元 · 止盈 300.00 元");
  assert.equal(stopsText({ autoStopLoss: 100, status: "filled", autoStopApplied: false }, 0), "");
});
