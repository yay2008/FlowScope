/* 自研合约二级级联菜单(不用原生 <select>)。
 *
 * 结构: 一级 = 品种(交易所吸顶分组, 按持仓量降序), 二级 = 该品种的月份(第一项恒为★主力)。
 *       一级上面是分类页签(期货 / 加密), 一次只列一个分类; 只有一个分类时不显示页签。
 * 交互: 悬停/点击品种即出月份; 点击月份即切换; 键盘 ↑↓ 移动、→/← 换列、Tab/Shift+Tab 换分类、
 *       Enter 确认、Esc 关闭; 打开菜单时停在当前合约所属的分类;
 *       搜索框输入即过滤当前分类, 当前分类没命中而别的分类有时自动跳过去, 二级自动跟到第一个命中品种;
 *       每行的 ☆ 直接加/移出自选。
 * 数据: 一级 GET /api/symbols(服务端缓存 5 分钟), 二级 GET /api/symbols?exchange=&product=
 *       (服务端缓存 1 分钟), 前端再加内存缓存 + localStorage, 悬停即预取, 菜单基本都是秒开。
 * 依赖: picker-core.js(纯逻辑) + data-sync.js(标签格式化), 由 index.html 在它之前加载。
 * 自选由宿主页面持有: 传 isFavorite / onToggleFavorite 才显示 ☆, 改完由宿主调 refreshFavorites()。
 */
(function (root) {
  "use strict";

  const CATALOG_CACHE_KEY = "flowscope.catalog.v1";
  const HOVER_DEBOUNCE_MS = 120;
  const MONTHS_RETRY_MS = 8000;    // 月份取失败后隔多久允许再试(下次悬停/打开菜单时生效)
  const CATALOG_RETRY_MS = 10000;  // 目录失败后的自动重试间隔
  const CATALOG_RETRY_MAX = 6;

  function create(options) {
    const core = options.core || root.PickerCore;
    const flow = options.flow || root.FlowData;
    const doc = options.document || root.document;
    const fetchJson = options.fetchJson;
    const onPick = options.onPick || function () {};
    const onStatus = options.onStatus || function () {};
    const isFavorite = options.isFavorite || null;
    const onToggleFavorite = options.onToggleFavorite || null;
    const storage = options.storage || null;
    const el = options.root;
    const now = options.now || (() => Date.now());

    let products = [];               // 展平后的一级列表(全部分类)
    let categories = [];             // 目录里有品种的分类 id, 按 core.CATEGORIES 的顺序
    let category = "";               // 当前页签; 目录到之前为空串(= 不按分类过滤)
    let filtered = [];               // 当前分类里的搜索命中
    let productIndex = 0;
    let level = "product";           // "product" | "month"
    let monthIndex = 0;
    let activeKey = "";              // 二级正在展示的品种键 "SHFE.fu"
    let monthState = new Map();      // key -> {status, months, error}
    let symbol = "";                 // 当前合约(URL 里那个)
    let generation = 0;              // 请求代际: 丢弃过期响应
    let hoverTimer = null;
    let isOpen = false;
    let productRows = [];            // 与 filtered 一一对应, 供高亮时直接改 class
    let monthRows = [];              // 与 currentOptions() 一一对应
    // 两列各自记 ☆ 元素(就地改状态, 不重排列表, 免得滚动位置被重置), key = 合约代码
    let productStars = new Map();
    let monthStars = new Map();
    const pendingFavorites = new Set();   // 正在提交的自选, 免得连点发出重复请求

    const productKey = (product) => (product ? `${product.exchangeId}.${product.productId}` : "");
    const query = () => (el.search ? el.search.value || "" : "");
    const visibleProducts = () => core.filterProducts(products, query(), category);
    const activeProduct = () => filtered[productIndex] || null;
    const clear = (node) => { node.textContent = ""; };   // 清空子节点

    function node(className, role) {
      const item = doc.createElement("div");
      if (className) item.className = className;
      if (role) item.setAttribute("role", role);
      return item;
    }

    function setStatus(text, detail) {
      onStatus(text || "", detail || "");
    }

    // ---------- 目录 ----------

    function load(attempt = 0) {
      const cached = readCache();
      if (cached) applyCatalog(cached);
      fetchJson("/api/symbols")
        .then((data) => {
          applyCatalog(data);
          writeCache(data);
        })
        .catch((error) => {
          if (cached) return;                      // 有缓存先用着, 后台下次打开再刷新
          setStatus(`合约目录加载失败（${attempt + 1}/${CATALOG_RETRY_MAX}），可直接手填合约`,
                    error.message);
          // 连接刚断时目录会失败, 稍后自动重试; 不重试的话这一页就只能手填合约了。
          if (attempt + 1 < CATALOG_RETRY_MAX) {
            setTimeout(() => load(attempt + 1), CATALOG_RETRY_MS);
          }
        });
    }

    function applyCatalog(data) {
      generation += 1;                                  // 上一份目录的月份请求全部作废
      // 常见时序: 先用缓存画出菜单, 用户已经 ↑↓ 选到某个品种, 网络目录才到。光标要跟着
      // 那个品种走(新目录里它可能换了位置), 不能归零 —— 否则紧接着的 Enter 会切到第一个品种。
      const keepKey = productKey(activeProduct());
      const keepLevel = level;
      const keepMonth = monthIndex;
      // 已经取到的月份也带过去, 否则二级在重取完成前只剩「★ 主力」, 月份光标会落到列表之外。
      // 只带成功的结果: 取到一半的请求会因为换代被丢弃, 带过去的 loading 状态就永远不会结束。
      const keepMonths = monthState.get(keepKey);
      monthState = new Map();
      if (keepMonths && keepMonths.status === "ready" && !keepMonths.error) {
        monthState.set(keepKey, keepMonths);
      }
      products = core.flattenGroups((data && data.groups) || []);
      categories = core.CATEGORIES.map((item) => item.id)
        .filter((id) => products.some((product) => core.categoryOf(product) === id));
      // 首次到达时按当前合约选页签; 当前页签在新目录里没了(加密源停了)也重新选
      if (!categories.includes(category)) category = symbolCategory() || categories[0] || "";
      productIndex = 0;
      level = "product";
      monthIndex = 0;
      activeKey = "";
      if (keepKey) {
        const index = visibleProducts().findIndex((item) => productKey(item) === keepKey);
        if (index >= 0) {
          productIndex = index;
          level = keepLevel;
          monthIndex = keepMonth;
          activeKey = keepKey;                          // 同一品种: followActive 不必再把月份光标归零
        }
      }
      applyFilter(false);
      if (level === "month") {
        const last = Math.max(0, currentOptions().length - 1);
        if (monthIndex > last) {
          monthIndex = last;
          renderHighlight();
        }
      }
      if (data && data.source === "fallback") {
        setStatus("目录: 离线常用品种", data.error || "");
      } else if (data && data.error) {
        setStatus("", data.error);
      } else {
        setStatus("");
      }
    }

    function readCache() {
      if (!storage) return null;
      try {
        const cached = JSON.parse(storage.getItem(CATALOG_CACHE_KEY) || "null");
        return cached && Array.isArray(cached.groups) && cached.groups.length ? cached : null;
      } catch (error) {
        return null;
      }
    }

    function writeCache(data) {
      if (!storage || !data || !data.groups || !data.groups.length) return;
      try {
        storage.setItem(CATALOG_CACHE_KEY, JSON.stringify({ at: now(), groups: data.groups }));
      } catch (error) { /* 存不下就算了, 只是下次打开慢一点 */ }
    }

    // ---------- 过滤与渲染 ----------

    function applyFilter(reset) {
      filtered = visibleProducts();
      if (reset) {
        productIndex = 0;
        level = "product";
        monthIndex = 0;
      }
      productIndex = Math.min(productIndex, Math.max(0, filtered.length - 1));
      followActive(true);
      renderTabs();
      renderProducts();
      renderMonths();
      renderHighlight();
    }

    // ---------- 分类页签 ----------

    // 当前合约属于哪个分类: 主连/永续按代码直接找, 具体月份按 交易所.品种 找; 找不到返回空串。
    function symbolCategory() {
      if (!symbol) return "";
      const parts = flow.parseSymbolParts(symbol);
      const product = products.find((item) => item.contSymbol === symbol
        || (parts && item.exchangeId === parts.exchange && item.productId === parts.product));
      return product ? core.categoryOf(product) : "";
    }

    function setCategory(id) {
      if (!id || id === category || !categories.includes(id)) return;
      category = id;
      applyFilter(true);
    }

    function stepCategory(delta) {
      const count = categories.length;
      const index = categories.indexOf(category);
      setCategory(categories[((index + delta) % count + count) % count]);
    }

    // 搜索时当前分类没命中、别的分类有: 直接跳过去(期货页输入 btc 就到加密页)。
    // 只在输入时跳; 用户自己点到一个没命中的页签不跳回去, 清空搜索也不跳。
    function followMatches() {
      if (!query().trim()) return;
      const counts = core.countByCategory(products, query());
      if (counts[category]) return;
      const next = categories.find((id) => counts[id]);
      if (next) category = next;
    }

    function renderTabs() {
      if (!el.tabs) return;
      clear(el.tabs);
      el.tabs.hidden = categories.length < 2;
      if (el.tabs.hidden) return;
      const searching = !!query().trim();
      const counts = searching ? core.countByCategory(products, query()) : null;
      for (const item of core.CATEGORIES) {
        if (!categories.includes(item.id)) continue;
        const active = item.id === category;
        const tab = node("picker-tab" + (active ? " active" : ""), "tab");
        tab.setAttribute("aria-selected", active ? "true" : "false");
        const label = doc.createElement("span");
        label.textContent = item.label;
        tab.append(label);
        if (counts) {
          const count = doc.createElement("span");
          count.className = "picker-tab-count";
          count.textContent = String(counts[item.id] || 0);
          tab.append(count);
        }
        // 按下不抢走搜索框的焦点, 切完分类还能接着用键盘
        tab.addEventListener("mousedown", (event) => event.preventDefault());
        tab.addEventListener("click", (event) => {
          // 页签会随过滤重画, 点中的元素已经不在菜单里了; 不拦住的话"点外部收起"会把菜单关掉
          event.stopPropagation();
          setCategory(item.id);
        });
        el.tabs.append(tab);
      }
    }

    // 二级始终跟着"当前高亮品种"; 换了品种就取(或预取)它的月份。
    // 鼠标扫过整个列表时只高亮、不请求, 停住 120ms 才取 —— 免得一路划过就发一串查询。
    function followActive(fetchNow) {
      const product = activeProduct();
      const key = productKey(product);
      if (key !== activeKey) {
        activeKey = key;
        monthIndex = 0;
      }
      if (!product) return;
      if (fetchNow) ensureMonths(product);
      else scheduleMonths(product);
    }

    function scheduleMonths(product) {
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => ensureMonths(product), HOVER_DEBOUNCE_MS);
    }

    function ensureMonths(product) {
      const key = productKey(product);
      // 加密永续没有月份: 二级只有永续本身(monthOptions 的第一项), 不必去问服务端
      if (product.crypto) {
        if (!monthState.has(key)) monthState.set(key, { status: "ready", months: [], error: null, at: now() });
        return;
      }
      const known = monthState.get(key);
      if (known) {
        // 取失败不是永久状态: 隔一会儿(下次悬停/打开/搜索命中它时)再试一次
        // 服务端把月份查询失败(超时、连接无响应)包成 200 + monthsError 返回, 也算失败
        const failed = known.status === "error" || (known.status === "ready" && known.error);
        const retryable = failed && now() - (known.at || 0) >= MONTHS_RETRY_MS;
        if (!retryable) return;
      }
      const ticket = generation;
      monthState.set(key, { status: "loading", months: (known && known.months) || [],
                            error: null, at: now() });
      if (key === activeKey) renderMonths();
      fetchJson(`/api/symbols?exchange=${encodeURIComponent(product.exchangeId)}` +
                `&product=${encodeURIComponent(product.productId)}`)
        .then((data) => {
          if (ticket !== generation) return;            // 目录已换代, 这份响应过期了
          monthState.set(key, { status: "ready", months: (data && data.months) || [],
                                error: (data && data.monthsError) || null, at: now() });
          if (key === activeKey) { renderMonths(); renderHighlight(); }
        })
        .catch((error) => {
          if (ticket !== generation) return;
          monthState.set(key, { status: "error", months: [], error: error.message, at: now() });
          if (key === activeKey) { renderMonths(); renderHighlight(); }
        });
    }

    function renderProducts() {
      clear(el.products);
      productRows = [];
      productStars = new Map();
      if (!filtered.length) {
        const empty = node("picker-empty");
        empty.textContent = "没有匹配的品种";
        el.products.append(empty);
        return;
      }
      let exchange = null;
      filtered.forEach((product, index) => {
        if (product.exchangeName !== exchange) {
          exchange = product.exchangeName;
          const header = node("picker-group");
          header.textContent = exchange;
          header.setAttribute("aria-hidden", "true");
          el.products.append(header);
        }
        const item = node("picker-row", "option");
        item.setAttribute("id", `picker-product-${index}`);
        const name = doc.createElement("span");
        name.className = "picker-name";
        name.textContent = flow.productLabel(product);
        name.title = `${product.contSymbol}${product.mainSymbol ? " → " + product.mainSymbol : ""}`;
        const qty = doc.createElement("span");
        qty.className = "picker-qty";
        // 加密永续的 24 小时成交额要等交易所合约列表到了才有, 没有就空着(不显示 0)
        qty.textContent = product.crypto && !product.openInterest ? "" : flow.formatOpenInterest(product.openInterest);
        // 期货这一栏是主力合约的昨日持仓量, 加密永续是 24 小时成交额(USDT)
        qty.title = product.crypto ? "24 小时成交额（USDT）" : "主力合约昨日持仓量";
        item.append(name, qty);
        // 品种行上的 ☆ 收藏的是它的主力(主连), 与二级第一项是同一个合约; 加密永续就是它本身
        appendStar(item, product.contSymbol, product.crypto ? "" : "该品种主力", productStars);
        item.addEventListener("mouseenter", () => selectProduct(index, false));
        item.addEventListener("click", () => selectProduct(index, true));
        item.addEventListener("dblclick", () => pick(product.contSymbol));   // 双击 = 直接主力
        el.products.append(item);
        productRows.push(item);
      });
    }

    function selectProduct(index, fetchNow) {
      const changed = index !== productIndex;
      productIndex = index;
      level = "product";
      if (changed) {
        monthIndex = 0;
        followActive(fetchNow);
        renderMonths();
      }
      renderHighlight();
    }

    function currentOptions() {
      const product = activeProduct();
      if (!product) return [];
      const state = monthState.get(productKey(product));
      return core.monthOptions(product, (state && state.months) || []);
    }

    function renderMonths() {
      clear(el.months);
      monthRows = [];
      monthStars = new Map();
      const product = activeProduct();
      if (!product) return;
      const state = monthState.get(productKey(product));
      const head = node("picker-months-head");
      head.textContent = `${product.crypto ? "永续" : "月份"} · ${product.name}` +
        (state && state.status === "loading" ? "（加载中…）" : "");
      el.months.append(head);
      core.monthOptions(product, (state && state.months) || []).forEach((option, index) => {
        const item = node("picker-row picker-row-month", "option");
        item.setAttribute("id", `picker-month-${index}`);
        const name = doc.createElement("span");
        name.className = "picker-name";
        name.textContent = product.crypto ? `${product.name} · ${product.exchangeName}`
          : (option.isMain ? "★ " : "") + flow.monthLabel(option);
        name.title = option.symbol;
        item.append(name);
        appendStar(item, option.symbol, option.isMain && !product.crypto ? "该品种主力" : "", monthStars);
        item.addEventListener("mouseenter", () => {
          level = "month";
          monthIndex = index;
          renderHighlight();
        });
        item.addEventListener("click", () => pick(option.symbol));
        el.months.append(item);
        monthRows.push(item);
      });
      if (state && state.error) {
        const hint = node("picker-empty");
        hint.textContent = "月份列表暂不可用（稍后自动重试）：" + state.error;
        el.months.append(hint);
      }
    }

    // ---------- 行内自选(☆) ----------
    // 收藏哪一行由宿主页面负责落库; 这里只管点击、乐观置灰和状态重绘。
    // 星号单独占一个 span: 点它不切换合约、不关菜单, 可以连着收藏几个。

    function appendStar(item, symbol, extra, registry) {
      if (!isFavorite || !onToggleFavorite || !symbol) return;
      const star = doc.createElement("span");
      star.setAttribute("role", "button");
      star.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleFavorite(symbol);
      });
      star.addEventListener("dblclick", (event) => event.stopPropagation());
      star.starExtra = extra;          // 重画时沿用这一行自己的提示
      paintStar(star, symbol, extra);
      item.append(star);
      const nodes = registry.get(symbol) || [];
      nodes.push(star);
      registry.set(symbol, nodes);
    }

    function paintStar(star, symbol, extra = "") {
      const starred = !!isFavorite(symbol);
      star.textContent = starred ? "★" : "☆";
      star.className = "picker-star" + (starred ? " on" : "") +
        (pendingFavorites.has(symbol) ? " pending" : "");
      star.title = [starred ? "移出自选" : "加入自选", extra].filter(Boolean).join(" · ");
      star.setAttribute("aria-label", `${starred ? "移出自选" : "加入自选"} ${symbol}`);
    }

    function toggleFavorite(symbol) {
      if (pendingFavorites.has(symbol)) return;
      pendingFavorites.add(symbol);
      refreshFavorites();
      Promise.resolve(onToggleFavorite(symbol))
        .catch(() => { /* 失败原因由宿主页面提示 */ })
        .then(() => {
          pendingFavorites.delete(symbol);
          refreshFavorites();
        });
    }

    // 宿主页面改了自选(行内点击、自选面板 ×)后调这个, 就地重画所有 ☆
    function refreshFavorites() {
      for (const registry of [productStars, monthStars]) {
        for (const [symbol, nodes] of registry) {
          nodes.forEach((star) => paintStar(star, symbol, star.starExtra));
        }
      }
    }

    function renderHighlight() {
      productRows.forEach((item, index) => {
        const active = index === productIndex;
        item.className = "picker-row" + (active ? " active" : "");
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
      monthRows.forEach((item, index) => {
        // 二级只有真正在键盘/鼠标聚焦时才高亮, 免得和一级同时亮两行
        const active = level === "month" && index === monthIndex;
        item.className = "picker-row picker-row-month" + (active ? " active" : "");
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
      if (el.search) {
        el.search.setAttribute("aria-activedescendant",
          level === "month" ? `picker-month-${monthIndex}` : `picker-product-${productIndex}`);
      }
      if (el.trigger) {
        el.trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
        // 目录/月份到手后触发器要从原始代码升级成「品种 · 主力月份」
        el.trigger.textContent = triggerLabel();
      }
    }

    // ---------- 打开 / 关闭 / 选中 ----------

    function open() {
      if (isOpen) return;
      isOpen = true;
      el.popup.hidden = false;
      if (el.trigger) el.trigger.setAttribute("aria-expanded", "true");
      el.search.focus();
      // 每次打开都回到当前合约所属的分类(看 fu 打开是期货, 看 BTC 打开是加密)
      const home = symbolCategory();
      const moved = !!home && home !== category && categories.includes(home);
      if (moved) category = home;
      applyFilter(moved);
    }

    function close() {
      if (!isOpen) return;
      isOpen = false;
      el.popup.hidden = true;
      if (el.trigger) el.trigger.setAttribute("aria-expanded", "false");
    }

    function pick(next) {
      close();
      if (next && next !== symbol) onPick(next);
    }

    function setSymbol(value) {
      symbol = value || "";
      if (el.trigger) el.trigger.textContent = triggerLabel();
    }

    // 触发器显示「品种 · 主力月份」; 具体月份显示其中文名; 目录还没到时先显示原始代码。
    function triggerLabel() {
      const parts = flow.parseSymbolParts(symbol);
      if (!parts) return symbol || "选择合约";
      const product = products.find((item) => item.exchangeId === parts.exchange
        && item.productId === parts.product);
      if (!product) return symbol;
      if (parts.isCont) return flow.productLabel(product);
      const state = monthState.get(productKey(product));
      const month = state && (state.months || []).find((item) => item.symbol === symbol);
      return (month && month.name) || symbol;
    }

    // ---------- 事件 ----------

    el.search.addEventListener("keydown", (event) => {
      const key = event.key === "Tab" && event.shiftKey ? "Shift+Tab" : event.key;
      const result = core.reduceKey({ level, productIndex, monthIndex }, key,
                                    filtered, currentOptions());
      // 只有一个分类(没有页签)时 Tab 照旧只收起菜单, 不拦住默认的焦点移动
      if (result.action.type === "category" && categories.length < 2) {
        close();
        return;
      }
      if (result.action.type !== "none") event.preventDefault();
      level = result.state.level;
      productIndex = result.state.productIndex;
      monthIndex = result.state.monthIndex;
      if (result.action.type === "prefetch") {
        followActive(true);
        renderMonths();
        renderHighlight();
      } else if (result.action.type === "pick") {
        pick(result.action.symbol);
      } else if (result.action.type === "category") {
        stepCategory(result.action.delta);
      } else if (result.action.type === "close") {
        close();
      } else {
        renderHighlight();
      }
    });
    el.search.addEventListener("input", () => {
      followMatches();
      applyFilter(true);
    });
    if (el.trigger) el.trigger.addEventListener("click", () => (isOpen ? close() : open()));
    doc.addEventListener("click", (event) => {
      // 点到菜单和触发器之外就收起; 触发器自己的点击已经 toggle 过, 这里不再处理。
      if (!isOpen || !el.wrapper || typeof el.wrapper.contains !== "function") return;
      if (el.wrapper.contains(event.target)) return;
      close();
    });

    return {
      load, setSymbol, open, close, refreshFavorites,
      isOpen: () => isOpen,
      // 给宿主页面与回归测试观察内部状态
      snapshot: () => ({ products: filtered, months: currentOptions(), level, productIndex,
                         monthIndex, category, categories: categories.slice(),
                         label: el.trigger ? el.trigger.textContent : "" }),
    };
  }

  const api = { create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ContractPicker = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
