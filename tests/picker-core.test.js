"use strict";
/* picker-core 的纯逻辑回归: 展平、搜索、月份选项、键盘状态机。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { flattenGroups, filterProducts, monthOptions, moveIndex, reduceKey } =
  require("../static/picker-core.js");

const GROUPS = [
  { exchangeId: "SHFE", exchangeName: "上期所", products: [
    { exchangeId: "SHFE", productId: "fu", name: "燃油", contSymbol: "KQ.m@SHFE.fu",
      mainSymbol: "SHFE.fu2611", openInterest: 428100 },
    { exchangeId: "SHFE", productId: "rb", name: "螺纹", contSymbol: "KQ.m@SHFE.rb",
      mainSymbol: "SHFE.rb2701", openInterest: 3144530 },
  ] },
  { exchangeId: "DCE", exchangeName: "大商所", products: [
    { exchangeId: "DCE", productId: "i", name: "铁矿石", contSymbol: "KQ.m@DCE.i",
      mainSymbol: "DCE.i2701", openInterest: 590347 },
  ] },
];
const PRODUCTS = flattenGroups(GROUPS);
const MONTHS = [
  { symbol: "SHFE.fu2611", name: "燃油2611", openInterest: 428100, isMain: true },
  { symbol: "SHFE.fu2701", name: "燃油2701", openInterest: 266684, isMain: false },
];

test("目录展平后保留交易所名与原始顺序", () => {
  assert.deepEqual(PRODUCTS.map((p) => p.productId), ["fu", "rb", "i"]);
  assert.deepEqual(PRODUCTS.map((p) => p.exchangeName), ["上期所", "上期所", "大商所"]);
  assert.deepEqual(flattenGroups(null), []);
});

test("搜索匹配中文名、品种代码、主连代码、主力合约与交易所", () => {
  assert.deepEqual(filterProducts(PRODUCTS, "燃油").map((p) => p.productId), ["fu"]);
  assert.deepEqual(filterProducts(PRODUCTS, "rb").map((p) => p.productId), ["rb"]);
  assert.deepEqual(filterProducts(PRODUCTS, "KQ.m@DCE.i").map((p) => p.productId), ["i"]);
  assert.deepEqual(filterProducts(PRODUCTS, "2701").map((p) => p.productId), ["rb", "i"]);
  assert.deepEqual(filterProducts(PRODUCTS, "shfe").map((p) => p.productId), ["fu", "rb"]);
  assert.deepEqual(filterProducts(PRODUCTS, "  ").length, 3);   // 空搜索 = 全部
  assert.deepEqual(filterProducts(PRODUCTS, "不存在"), []);
});

test("二级第一项恒为★主力, 即使月份列表为空或还没回来", () => {
  const withMonths = monthOptions(PRODUCTS[0], MONTHS);
  assert.deepEqual(withMonths.map((o) => o.symbol), ["KQ.m@SHFE.fu", "SHFE.fu2701"]);
  assert.equal(withMonths[0].isMain, true);
  assert.equal(withMonths[0].openInterest, 428100);
  const withoutMonths = monthOptions(PRODUCTS[0], []);
  assert.deepEqual(withoutMonths.map((o) => o.symbol), ["KQ.m@SHFE.fu"]);
  assert.deepEqual(monthOptions(null, MONTHS), []);
});

test("上下键到边界停住, 不绕回", () => {
  assert.equal(moveIndex(0, -1, 3), 0);
  assert.equal(moveIndex(2, 1, 3), 2);
  assert.equal(moveIndex(1, 1, 3), 2);
  assert.equal(moveIndex(0, 1, 0), 0);
});

test("一级上下键移动并请求预取对应品种的月份", () => {
  const state = { level: "product", productIndex: 0, monthIndex: 0 };
  const down = reduceKey(state, "ArrowDown", PRODUCTS, []);
  assert.equal(down.state.productIndex, 1);
  assert.deepEqual(down.action, { type: "prefetch", index: 1 });
  const top = reduceKey(state, "ArrowUp", PRODUCTS, []);
  assert.equal(top.state.productIndex, 0);
  assert.deepEqual(top.action, { type: "none" });   // 已经在第一条, 不动也不预取
  const end = reduceKey(state, "End", PRODUCTS, []);
  assert.equal(end.state.productIndex, 2);
  assert.deepEqual(end.action, { type: "prefetch", index: 2 });
});

test("左右键在一二级之间切换, 不会越界", () => {
  const state = { level: "product", productIndex: 1, monthIndex: 0 };
  const right = reduceKey(state, "ArrowRight", PRODUCTS, MONTHS);
  assert.equal(right.state.level, "month");
  assert.equal(right.state.monthIndex, 0);
  const left = reduceKey(right.state, "ArrowLeft", PRODUCTS, MONTHS);
  assert.equal(left.state.level, "product");
  assert.equal(left.state.productIndex, 1);          // 回到一级还停在原品种
  const idle = reduceKey({ level: "product", productIndex: 0, monthIndex: 0 }, "ArrowLeft", PRODUCTS, MONTHS);
  assert.deepEqual(idle.action, { type: "none" });
});

test("回车在一级选主力、在二级选具体月份", () => {
  const productLevel = reduceKey({ level: "product", productIndex: 0, monthIndex: 0 }, "Enter", PRODUCTS, MONTHS);
  assert.deepEqual(productLevel.action, { type: "pick", symbol: "KQ.m@SHFE.fu" });
  const monthLevel = reduceKey({ level: "month", productIndex: 0, monthIndex: 1 }, "Enter", PRODUCTS, MONTHS);
  assert.deepEqual(monthLevel.action, { type: "pick", symbol: "SHFE.fu2701" });
  const empty = reduceKey({ level: "product", productIndex: 0, monthIndex: 0 }, "Enter", [], []);
  assert.deepEqual(empty.action, { type: "none" });
});

test("二级上下键移动月份, Esc/Tab 关闭菜单", () => {
  const state = { level: "month", productIndex: 0, monthIndex: 0 };
  const down = reduceKey(state, "ArrowDown", PRODUCTS, MONTHS);
  assert.equal(down.state.monthIndex, 1);
  assert.deepEqual(down.action, { type: "none" });   // 二级移动不需要重新取月份
  assert.equal(reduceKey(state, "ArrowDown", PRODUCTS, MONTHS).state.level, "month");
  for (const key of ["Escape", "Tab"]) {
    const result = reduceKey(state, key, PRODUCTS, MONTHS);
    assert.deepEqual(result.action, { type: "close" });
  }
  assert.deepEqual(reduceKey(state, "a", PRODUCTS, MONTHS).action, { type: "none" });
});
