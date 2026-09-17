/* 合约选择器的纯逻辑: 展平目录、搜索过滤、月份选项、键盘状态机。
 *
 * 不碰 DOM, 浏览器与 Node 回归测试共用(与 data-sync.js 同样的写法)。
 * 展示用的文字标签在 data-sync.js 里(productLabel / monthLabel), 这里只出数据。
 */
(function (root) {
  "use strict";

  const NO_ACTION = Object.freeze({ type: "none" });

  // 目录是 交易所 -> 品种 的两层结构; 选择器要按"一行一个品种"来导航和搜索, 所以先展平。
  function flattenGroups(groups) {
    const products = [];
    for (const group of groups || []) {
      const exchangeName = group.exchangeName || group.exchangeId || "";
      for (const product of (group && group.products) || []) {
        products.push({ ...product, exchangeName });
      }
    }
    return products;
  }

  function normalize(value) {
    return value == null ? "" : String(value).trim().toLowerCase();
  }

  // 搜索口径: 中文名、品种代码、主连代码、主力合约代码、交易所代码。
  function filterProducts(products, query) {
    const needle = normalize(query);
    if (!needle) return (products || []).slice();
    return (products || []).filter((product) =>
      normalize(product.name).includes(needle) ||
      normalize(product.productId).includes(needle) ||
      normalize(product.contSymbol).includes(needle) ||
      normalize(product.mainSymbol).includes(needle) ||
      normalize(product.exchangeId).includes(needle));
  }

  // 二级菜单: 第一项恒为「主力」, 即使月份列表还没回来(或请求失败)也能选主力。
  function monthOptions(product, months) {
    if (!product) return [];
    const list = months || [];
    const main = list.find((month) => month.isMain) || null;
    const options = [{
      symbol: product.contSymbol,
      name: (main && main.name) || product.name || product.contSymbol || "主力",
      openInterest: main ? main.openInterest : product.openInterest,
      isMain: true,
    }];
    for (const month of list) {
      if (month.isMain) continue;      // 主力已经在第一项, 不重复列出
      options.push({ symbol: month.symbol, name: month.name, openInterest: month.openInterest,
                     isMain: false });
    }
    return options;
  }

  // 上下键到边界就停住(不绕回), 与行情软件的列表一致。
  function moveIndex(index, delta, length) {
    if (!length) return 0;
    const next = index + delta;
    return next < 0 ? 0 : next > length - 1 ? length - 1 : next;
  }

  /*
   * 键盘状态机: state = {level, productIndex, monthIndex}, 返回 {state, action}。
   * action 只有四种, 由调用方落地:
   *   {type:"none"}                 什么都不做
   *   {type:"prefetch", index}      一级停到了新品种, 去取它的月份
   *   {type:"pick", symbol}         选定一个合约(一级回车 = 该品种主力)
   *   {type:"close"}                关闭菜单
   */
  function reduceKey(state, key, products, months) {
    const list = products || [];
    const monthList = months || [];
    const same = { state, action: NO_ACTION };
    switch (key) {
      case "ArrowDown":
      case "ArrowUp": {
        const delta = key === "ArrowDown" ? 1 : -1;
        if (state.level === "month") {
          return { state: { ...state, monthIndex: moveIndex(state.monthIndex, delta, monthList.length) },
                   action: NO_ACTION };
        }
        const index = moveIndex(state.productIndex, delta, list.length);
        if (index === state.productIndex) return same;
        return { state: { ...state, productIndex: index }, action: { type: "prefetch", index } };
      }
      case "Home":
      case "End": {
        const index = key === "Home" ? 0 : Math.max(0, list.length - 1);
        if (state.level === "month") return { state: { ...state, monthIndex: index }, action: NO_ACTION };
        if (index === state.productIndex) return same;
        return { state: { ...state, productIndex: index }, action: { type: "prefetch", index } };
      }
      case "ArrowRight":
        if (state.level === "product" && list.length) {
          return { state: { ...state, level: "month", monthIndex: 0 }, action: NO_ACTION };
        }
        return same;
      case "ArrowLeft":
        if (state.level === "month") return { state: { ...state, level: "product" }, action: NO_ACTION };
        return same;
      case "Enter": {
        if (state.level === "month") {
          const month = monthList[state.monthIndex];
          return month ? { state, action: { type: "pick", symbol: month.symbol } } : same;
        }
        const product = list[state.productIndex];
        // 一级直接回车 = 该品种的主力(主连), 不用先进二级, 这是最常用的路径。
        return product ? { state, action: { type: "pick", symbol: product.contSymbol } } : same;
      }
      case "Escape":
      case "Tab":
        return { state, action: { type: "close" } };
      default:
        return same;
    }
  }

  const api = { flattenGroups, filterProducts, monthOptions, moveIndex, reduceKey };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PickerCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
