/* 模拟交易面板(右侧): 下单、持仓、账户、委托与成交; 以及图表上的成交标记和价格线的数据。
 *
 * 后端撮合在 paper.py: 市价单按对手价整笔成交, 限价单要价格穿过才成交(看不到排队), 条件单(突破单)
 * 最新价碰到触发价后按市价成交。
 * 加密合约是另一个账户(paper_crypto.py, state.mode === "crypto"): 数量是小数的币、USDT 记账、
 * 每个合约一个杠杆、计资金费; 面板据此换单位、显示杠杆选择与强平价。
 * 止盈止损挂在持仓上(每个合约一对价格, 按最新价触发后市价全平, 由服务端每轮检查), 在「持仓」下方设置;
 * 下单表单里也能填, 跟着委托走, 成交后由服务端挂到持仓上。K 线上还能划线设置(拖动在 chart-view.js,
 * 这里出拖动时线的价位、标题与预估盈亏, 松手后由 setStops 提交)。挂单线也能拖(改价, amendOrder)、双击撤单
 * (cancelOrder); 点击十字线加号按选中的价位挂限价单或条件单(menuItems, 手数与止盈止损用下单表单里的)。
 * 这几个图上操作的结果回 {ok, text}, 图上就地提示 —— 面板折叠着也看得到。
 * 面板轮询 GET /api/paper(展开时 1 秒一次, 折叠时 5 秒一次, 页面在后台时停止),
 * 每次拿到新数据回调 onState, 由 app.js 重画成交标记与价格线。
 * 纯函数(buildMarkers / priceLines / 格式化)在 Node 测试里直接加载。
 */
(function (root) {
  "use strict";
  const FlowDataRef = typeof module !== "undefined" && module.exports ? require("./data-sync.js") : root.FlowData;

  // 国内习惯: 红 = 买/多/盈, 绿 = 卖/空/亏(与自选面板的红涨绿跌一致)
  const BUY_COLOR = "#f23645";
  const SELL_COLOR = "#00b36b";
  // 止盈止损线避开红绿, 免得和持仓均价、挂单线混在一起
  const TP_COLOR = "#ffa726";
  const SL_COLOR = "#ab47bc";
  // 触发出来的平仓单: 委托列表里价格处写原因, 不写「市价」
  const TRIGGER_REASONS = new Set(["止盈", "止损", "强平"]);
  const POLL_MS = 1000;
  const POLL_COLLAPSED_MS = 5000;
  const COLLAPSED_KEY = "flowscope.tradeCollapsed";
  const AUTO_STOPS_KEY = "flowscope.futuresAutoStops";
  const CRYPTO_SIZING_KEY = "flowscope.cryptoSizing";
  const AUTO_STOPS_VERSION = 2;
  const STATUS_LABELS = { open: "挂单中", filled: "已成交", cancelled: "已撤", rejected: "已拒" };
  const LEVERAGES = [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 125];
  const STOP_NAMES = { tp: "止盈", sl: "止损" };
  const ORDER_KEY = "order:";   // 图上划线认挂单线: "order:" + 委托编号

  // 成交时间 -> 所在 bar 的时间: 标记只能打在已有的 bar 上, 落在休市缺口里的挂到前一根。
  function snapTime(bars, time) {
    if (!bars.length || time < bars[0].time) return null;
    let lo = 0, hi = bars.length - 1, ans = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (bars[mid].time <= time) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return bars[ans].time;
  }

  // 成交标记: 只写一个字母, 买在 K 线下方红色「B」, 卖在上方绿色「S」; 早于图表第一根的不画。
  // size 0: 库不画形状(箭头/圆点), 只剩文字; shape 是库要求的必填项, 不起作用。
  function buildMarkers(trades, bars) {
    const markers = [];
    for (const trade of trades || []) {
      const time = snapTime(bars, trade.time);
      if (time == null) continue;
      const buy = trade.side === "buy";
      markers.push({ time, position: buy ? "belowBar" : "aboveBar", shape: "circle", size: 0,
                     color: buy ? BUY_COLOR : SELL_COLOR, text: buy ? "B" : "S" });
    }
    return markers.sort((a, b) => a.time - b.time);   // 库要求标记按时间升序
  }

  function currentPositionOf(state) {
    const contract = state && state.contract;
    return contract ? (state.positions || []).find((item) => item.contract === contract) || null : null;
  }

  function openOrdersOf(state) {
    const contract = state && state.contract;
    return contract ? (state.orders || []).filter((order) => order.status === "open" && order.contract === contract) : [];
  }

  // 挂单线的标题: 限价单「买2 限价」, 条件单「买2 条件」(价格在价格轴标签上)
  function orderLineTitle(order) {
    return `${order.side === "buy" ? "买" : "卖"}${order.qty} ${order.type === "stop" ? "条件" : "限价"}`;
  }

  // 当前合约的持仓均价(实线)、止盈止损价(点线)与挂单价(限价虚线、条件长虚线); kind/id 给图上划线认线用
  function priceLines(state) {
    if (!state || !state.contract) return [];
    const lines = [];
    const position = currentPositionOf(state);
    if (position) {
      const long = position.qty > 0;
      lines.push({ kind: "avg", price: position.avgPrice, color: long ? BUY_COLOR : SELL_COLOR, style: "solid",
                   title: `${long ? "多" : "空"}${Math.abs(position.qty)} 均价` });
      if (position.tp != null) lines.push({ kind: "tp", price: position.tp, color: TP_COLOR, style: "dotted", title: "止盈" });
      if (position.sl != null) lines.push({ kind: "sl", price: position.sl, color: SL_COLOR, style: "dotted", title: "止损" });
    }
    for (const order of openOrdersOf(state)) {
      lines.push({ kind: "order", id: order.id, price: order.price, color: order.side === "buy" ? BUY_COLOR : SELL_COLOR,
                   style: order.type === "stop" ? "largeDashed" : "dashed", title: orderLineTitle(order) });
    }
    return lines;
  }

  // 价格线在划线里的名字: 止盈止损、均价按 kind, 挂单按 "order:" + 编号(dragTargets 的 from 与它对应)
  function lineKey(line) {
    return line.kind === "order" ? ORDER_KEY + line.id : line.kind;
  }

  // ---------- 划线止盈止损与挂单改价(图上的拖动交互在 chart-view.js) ----------

  // 图上能拖的线: 止盈、止损(改价), 挂单(改价), 持仓均价(从它拖出来新设止盈止损);
  // 均价线排最后, 和别的线叠在一起时先拖别的
  function dragTargets(state) {
    const position = currentPositionOf(state);
    const targets = position ? ["tp", "sl"].filter((kind) => position[kind] != null)
      .map((kind) => ({ from: kind, price: position[kind] })) : [];
    for (const order of openOrdersOf(state)) targets.push({ from: ORDER_KEY + order.id, price: order.price });
    if (position) targets.push({ from: "avg", price: position.avgPrice });
    return targets;
  }

  // 挂单改到 price 会怎样: 会当场成交(限价单够到对手价或越过最新价)、条件单触发价不在突破一侧时回原因,
  // 否则回空串。和服务端的改价校验一致, 拖线时先在标题上提示, 松手就不提交了。
  function amendProblem(state, order, price) {
    const quote = (state && state.quote) || {};
    const buy = order.side === "buy";
    const last = quote.last;
    if (order.type === "stop") {
      if (last == null) return "";
      return (buy ? price <= last : price >= last) ? `触发价要${buy ? "高" : "低"}于最新价` : "";
    }
    const opposite = buy ? quote.ask : quote.bid;
    const crosses = buy ? (opposite != null && price >= opposite) || (last != null && price > last)
      : (opposite != null && price <= opposite) || (last != null && price < last);
    return crosses ? "会立即成交" : "";
  }

  // 十字线加号选中 price 时能挂的单: 最新价下方是买入限价和卖出条件, 上方是卖出限价和买入条件,
  // 正好在最新价上(或还没有最新价)只给两个限价。
  function menuChoices(last, price) {
    if (last != null && price < last) return [{ side: "buy", type: "limit" }, { side: "sell", type: "stop" }];
    if (last != null && price > last) return [{ side: "sell", type: "limit" }, { side: "buy", type: "stop" }];
    return [{ side: "buy", type: "limit" }, { side: "sell", type: "limit" }];
  }

  function snapPrice(price, tick, decimals) {
    if (!(tick > 0)) return price;
    return Number((Math.round(price / tick) * tick).toFixed(decimals || 0));
  }

  // 拖到 rawPrice 时的那条线: 价位对齐最小变动价位; 从均价线拖出的按落点定止盈还是止损 ——
  // 在最新价的盈利一侧(多单在上、空单在下)是止盈, 另一侧是止损, 和服务端的校验一致。
  // 标题带上到这个价位平仓的预估盈亏(不含手续费; 期货乘合约乘数, 加密按币)。没有持仓返回 null。
  // 拖挂单线: 回 {kind: "order", id, price, problem}, 改不了(会当场成交等)时 problem 是原因, 标题上也写着。
  function dragLine(state, from, rawPrice) {
    if (!Number.isFinite(rawPrice)) return null;
    const quote = (state && state.quote) || {};
    if (from.startsWith(ORDER_KEY)) {
      const order = openOrdersOf(state).find((item) => item.id === from.slice(ORDER_KEY.length));
      if (!order) return null;
      const price = snapPrice(rawPrice, quote.priceTick, quote.priceDecs);
      const problem = amendProblem(state, order, price);
      return { kind: "order", id: order.id, price, problem, color: order.side === "buy" ? BUY_COLOR : SELL_COLOR,
               title: orderLineTitle(order) + (problem ? ` · ${problem}` : "") };
    }
    const position = currentPositionOf(state);
    if (!position) return null;
    const price = snapPrice(rawPrice, quote.priceTick, quote.priceDecs);
    const last = quote.last != null ? quote.last : position.last != null ? position.last : position.avgPrice;
    const kind = from !== "avg" ? from : (position.qty > 0 ? price > last : price < last) ? "tp" : "sl";
    const pnl = (price - position.avgPrice) * position.qty * (position.multiplier || 1);
    return { kind, price, color: kind === "tp" ? TP_COLOR : SL_COLOR,
             title: `${STOP_NAMES[kind]} ${formatSigned(pnl, state.mode === "crypto" ? 2 : 0)}` };
  }

  function formatMoney(value, digits = 0) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return Number(value).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function formatSigned(value, digits = 0) {
    const text = formatMoney(value, digits);
    return value > 0 ? "+" + text : text;
  }

  function pnlClass(value) {
    return value > 0 ? "up" : value < 0 ? "down" : "";
  }

  function orderLabel(order, decimals) {
    const side = order.side === "buy" ? "买" : "卖";
    const price = order.type === "limit" ? `限 ${FlowDataRef.formatPrice(order.price, decimals)}`
      : order.type === "stop" ? `条件 ${order.side === "buy" ? "≥" : "≤"}${FlowDataRef.formatPrice(order.price, decimals)}`
      : TRIGGER_REASONS.has(order.reason) ? order.reason : "市价";
    return `${side} ${order.qty} ${price}`;
  }

  // 委托带的止盈止损(成交后挂到持仓上), 没带回空串
  function stopsText(order, decimals) {
    if (order.autoStopLoss != null || order.autoTakeProfit != null) {
      if (order.status === "filled" && order.autoStopApplied === false) return "";
      return [["自动止损", order.autoStopLoss], ["自动止盈", order.autoTakeProfit]]
        .filter(([, value]) => value != null).map(([label, value]) => `${label} ${formatMoney(value, 2)} 元`).join(" · ");
    }
    const parts = [];
    if (order.tp != null) parts.push(`止盈 ${FlowDataRef.formatPrice(order.tp, decimals)}`);
    if (order.sl != null) parts.push(`止损 ${FlowDataRef.formatPrice(order.sl, decimals)}`);
    return parts.join(" · ");
  }

  function newClientId() {
    const cryptoRef = root.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") return cryptoRef.randomUUID();
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function create(options) {
    const byId = options.byId;
    const fetchJson = options.fetchJson;
    const onState = options.onState || (() => {});
    const els = {
      panel: byId("trade"), toggle: byId("trade-toggle"), contract: byId("trade-contract"),
      ask: byId("trade-ask"), askVol: byId("trade-ask-vol"), bid: byId("trade-bid"), bidVol: byId("trade-bid-vol"),
      qty: byId("trade-qty"), type: byId("trade-type"), priceRow: byId("trade-price-row"), price: byId("trade-price"),
      priceLabel: byId("trade-price-label"),
      buy: byId("trade-buy"), sell: byId("trade-sell"), flatten: byId("trade-flatten"), msg: byId("trade-msg"),
      position: byId("trade-position"), account: byId("trade-account"), orders: byId("trade-orders"),
      trades: byId("trade-trades"), note: byId("trade-note"), reset: byId("trade-reset"),
      ordersCount: byId("trade-orders-count"),
      qtyLabel: byId("trade-qty-label"), leverageRow: byId("trade-leverage-row"), leverage: byId("trade-leverage"),
      qtyRow: byId("trade-qty-row"), sizingRow: byId("trade-sizing-row"), sizing: byId("trade-sizing"),
      marginRow: byId("trade-margin-row"), margin: byId("trade-margin"), sizingNote: byId("trade-sizing-note"),
      stops: byId("trade-stops"), tp: byId("trade-tp"), sl: byId("trade-sl"), stopsSet: byId("trade-stops-set"),
      stopsNote: byId("trade-stops-note"), orderTp: byId("trade-order-tp"), orderSl: byId("trade-order-sl"),
      orderStops: byId("trade-order-stops"), autoStops: byId("trade-auto-stops"),
      autoLossEnabled: byId("trade-auto-loss-enabled"), autoProfitEnabled: byId("trade-auto-profit-enabled"),
      autoLoss: byId("trade-auto-loss"), autoProfit: byId("trade-auto-profit"), autoNote: byId("trade-auto-note"),
      notice: byId("trade-notice"), noticeText: byId("trade-notice-text"), noticeClose: byId("trade-notice-close"),
    };
    let state = null;
    let timer = null;
    let polling = false;
    let refreshAgain = false;
    let submitting = false;
    let noticeTimer = null;
    const closeEvents = new Map();   // 两类账户分别去重; 初次加载只记基线, 不把历史平仓当作新通知。
    let pollError = false;   // 当前提示来自轮询失败: 下一次轮询成功就清掉, 下单结果的提示则保留
    let formMode = "";       // 下单表单当前按哪种账户布置(换了才重设数量默认值与杠杆选项)
    let stopsKey = null;     // 止盈止损输入框上次按哪份服务端数据回填(变了才回填, 不冲掉正在敲的数字)

    const collapsed = () => els.panel.classList.contains("collapsed");
    const orderType = () => (els.type.value === "limit" || els.type.value === "stop" ? els.type.value : "market");
    const decimals = () => (state && state.quote ? state.quote.priceDecs : 0);
    const price = (value) => FlowDataRef.formatPrice(value, decimals());
    const isCrypto = () => !!state && state.mode === "crypto";
    const byMargin = () => isCrypto() && els.sizing && els.sizing.value === "margin";
    // 金额: 期货按元取整, 加密按 USDT 两位小数; 数量: 期货是手, 加密是币(按数量步长的位数)
    const money = (value) => formatMoney(value, isCrypto() ? 2 : 0);
    const signed = (value) => formatSigned(value, isCrypto() ? 2 : 0);
    const amount = (value) => (isCrypto() && value != null
      ? FlowDataRef.formatPrice(value, state.quote ? state.quote.qtyDecs : 4) : String(value));
    const unit = () => (isCrypto() ? (state.quote && state.quote.unit) || "" : "手");
    const currentPosition = () => currentPositionOf(state);
    const readPrice = (input) => (input.value === "" ? null : Number(input.value));   // 空 = 不设

    function loadSizing() {
      let saved;
      try { saved = JSON.parse(root.localStorage.getItem(CRYPTO_SIZING_KEY)); } catch (error) { /* 用默认值 */ }
      els.sizing.value = saved && saved.mode === "qty" ? "qty" : "margin";
      const margin = saved && saved.margin;
      els.margin.value = Number.isFinite(margin) && margin > 0 ? String(margin) : "100";
    }

    function saveSizing() {
      const margin = Number(els.margin.value);
      try {
        root.localStorage.setItem(CRYPTO_SIZING_KEY, JSON.stringify({ mode: els.sizing.value,
          margin: Number.isFinite(margin) && margin > 0 ? margin : null }));
      } catch (error) { /* 存储不可用不影响下单 */ }
    }

    function renderSizingNote() {
      if (!els.sizingNote) return;
      els.sizingNote.hidden = !byMargin();
      if (!byMargin()) return;
      const margin = Number(els.margin.value);
      const notional = margin * state.leverage;
      const market = orderType() === "market";
      els.sizingNote.textContent = margin > 0 && Number.isFinite(notional)
        ? `仓位约 ${formatMoney(notional, 2)} USDT · ${state.leverage}x\n手续费另计 · ${market ? "币数向下取整" : "挂单后币数固定"}`
        : "请输入有效保证金金额";
      els.sizingNote.title = "币数按数量步长向下取整。市价按提交时五档盘口换算；限价/条件按委托价/触发价换算，" +
        "挂单后改价或改杠杆不重新换算，实际保证金可能变化。账户保证金按标记价格估值；反向下单先减仓。";
    }

    function loadAutoStops() {
      if (!els.autoLossEnabled) return;
      let saved;
      try { saved = JSON.parse(root.localStorage.getItem(AUTO_STOPS_KEY)); } catch (error) { /* 用默认值 */ }
      els.autoLossEnabled.checked = !saved || (saved.version === 2 ? saved.lossEnabled !== false : saved.enabled !== false);
      els.autoProfitEnabled.checked = !!saved && saved.version === 2 && saved.profitEnabled === true;
      const loss = saved && saved.loss;
      const profit = saved && saved.profit;
      els.autoLoss.value = Number.isFinite(loss) && loss > 0 ? String(loss) : "100";
      els.autoProfit.value = Number.isFinite(profit) && profit > 0 ? String(profit) : "150";
    }

    function renderAutoStops() {
      if (!els.autoLossEnabled) return;
      const enabled = !isCrypto() && (els.autoLossEnabled.checked || els.autoProfitEnabled.checked);
      els.autoStops.hidden = isCrypto();
      els.autoNote.hidden = !enabled;
      els.orderStops.hidden = enabled;
      els.autoLoss.disabled = !els.autoLossEnabled.checked;
      els.autoProfit.disabled = !els.autoProfitEnabled.checked;
      const unavailable = enabled && state && state.autoStopsVersion !== AUTO_STOPS_VERSION;
      els.autoNote.textContent = unavailable ? "请先重启服务，再使用自动止盈止损" : "整份持仓 · 不含手续费 · 价位取整";
      els.autoNote.classList.toggle("error", !!unavailable);
    }

    function changeAutoStops() {
      renderAutoStops();
      const loss = Number(els.autoLoss.value);
      const profit = Number(els.autoProfit.value);
      try {
        root.localStorage.setItem(AUTO_STOPS_KEY, JSON.stringify({ version: 2,
          lossEnabled: els.autoLossEnabled.checked, profitEnabled: els.autoProfitEnabled.checked,
          loss: Number.isFinite(loss) && loss > 0 ? loss : null,
          profit: Number.isFinite(profit) && profit > 0 ? profit : null }));
      } catch (error) { /* 存储不可用不影响下单 */ }
    }

    function setCollapsed(value) {
      try {
        root.localStorage.setItem(COLLAPSED_KEY, value ? "1" : "0");
      } catch (error) { /* 折叠状态记不住不影响使用 */ }
      els.panel.classList.toggle("collapsed", value);
      els.toggle.textContent = value ? "‹" : "›";
    }

    function readCollapsed() {
      try {
        return root.localStorage.getItem(COLLAPSED_KEY) === "1";
      } catch (error) {
        return false;
      }
    }

    function showMessage(text, kind = "") {
      els.msg.textContent = text || "";
      els.msg.className = "trade-msg" + (kind ? " " + kind : "");
    }

    function notifyAutomaticCloses() {
      const account = isCrypto() ? "crypto" : "futures";
      const orders = (state.orders || []).filter((order) => order.status === "filled");
      let events = closeEvents.get(account);
      if (!events) {
        events = { seen: new Set(orders.map((order) => order.id)), pending: [] };
        closeEvents.set(account, events);
        return;
      }
      for (const order of orders.slice().reverse()) {
        if (events.seen.has(order.id) || !TRIGGER_REASONS.has(order.reason)) continue;
        const trade = (state.trades || []).find((item) => item.orderId === order.id);
        const pnl = trade && Number.isFinite(trade.pnl)
          ? ` · 平仓盈亏 ${formatSigned(trade.pnl, 2)} ${isCrypto() ? "USDT" : "元"}（未扣手续费）` : "";
        events.pending.push(`${order.reason}已自动平仓：${order.contract} ` +
          `${order.side === "buy" ? "买入" : "卖出"} ${order.qty}${isCrypto() ? "" : " 手"} @${order.fillPrice}${pnl}`);
      }
      events.seen = new Set(orders.map((order) => order.id));
      if (!events.pending.length || submitting || state.error) return;
      const text = events.pending.splice(0).join("\n");
      showMessage(text, "ok");
      if (els.notice) {
        clearTimeout(noticeTimer);
        els.noticeText.textContent = text;
        els.notice.hidden = false;
        noticeTimer = setTimeout(() => { els.notice.hidden = true; }, 10000);
      }
    }

    function schedule(delay) {
      clearTimeout(timer);
      timer = null;
      if (root.document && root.document.hidden) return;
      timer = setTimeout(poll, delay == null ? (collapsed() ? POLL_COLLAPSED_MS : POLL_MS) : delay);
    }

    async function poll() {
      timer = null;
      if (polling) { refreshAgain = true; return; }
      polling = true;
      try {
        state = await fetchJson(`/api/paper?symbol=${encodeURIComponent(options.getSymbol())}`);
        if (pollError) { showMessage(""); pollError = false; }
        if (state.error && !submitting) { showMessage(state.error, "error"); pollError = true; }
        render();
        onState(state);
        notifyAutomaticCloses();
      } catch (error) {
        showMessage("模拟账户加载失败：" + error.message, "error");
        pollError = true;
      } finally {
        polling = false;
        schedule(refreshAgain ? 0 : undefined);
        refreshAgain = false;
      }
    }

    function grid(target, rows) {
      target.textContent = "";
      for (const [label, value, cls] of rows) {
        const name = root.document.createElement("span");
        name.className = "trade-dim";
        name.textContent = label;
        const text = root.document.createElement("span");
        text.className = cls || "";
        text.textContent = value;
        target.append(name, text);
      }
    }

    function renderQuote() {
      const quote = state.quote;
      const open = isCrypto() ? "24 小时交易" : "交易中";
      const closed = isCrypto() ? "暂停" : "休市";
      const status = quote ? (quote.open ? open : `${closed} · ${quote.reason}`) : "行情未就绪";
      els.contract.textContent = `${state.contract || state.symbol} · ${status}`;
      els.contract.title = quote ? `${quote.name}  报价时间 ${quote.datetime || "—"}` : "";
      if (isCrypto() && state.contract && state.symbol.startsWith("AGG.")) {
        els.contract.textContent = `币安撮合 · ${els.contract.textContent}`;
        els.contract.title = `多所汇总行情，模拟交易使用币安盘口，与币安合约共用持仓。${els.contract.title}`;
      }
      els.contract.classList.toggle("closed", !(quote && quote.open));
      els.ask.textContent = price(quote && quote.ask);
      els.askVol.textContent = quote && quote.askVolume != null ? amount(quote.askVolume) : "";
      els.bid.textContent = price(quote && quote.bid);
      els.bidVol.textContent = quote && quote.bidVolume != null ? amount(quote.bidVolume) : "";
      if (quote && quote.priceTick) {
        for (const input of [els.price, els.tp, els.sl, els.orderTp, els.orderSl]) input.step = String(quote.priceTick);
      }
      renderForm();
    }

    // 期货是整数手; 加密可按币数或 USDT 保证金下单, 保证金在服务端按当前杠杆换算。
    function renderForm() {
      const quote = state.quote;
      const mode = isCrypto() ? `crypto:${state.symbol}` : "futures";
      els.panel.classList.toggle("crypto", isCrypto());
      if (els.sizingRow) els.sizingRow.hidden = !isCrypto();
      if (els.qtyRow) els.qtyRow.hidden = !!byMargin();
      if (els.marginRow) els.marginRow.hidden = !byMargin();
      // 无法解析交易合约时没有报价: 不显示单位与杠杆
      if (els.qtyLabel) els.qtyLabel.textContent = isCrypto() ? (unit() ? `数量(${unit()})` : "数量") : "手数";
      if (els.leverageRow) els.leverageRow.hidden = !(isCrypto() && quote);
      if (isCrypto() && quote) {
        els.qty.step = String(quote.qtyStep);
        els.qty.min = String(quote.minQty);
        els.qty.max = "";
      } else if (!isCrypto()) {
        els.qty.step = "1";
        els.qty.min = "1";
        els.qty.max = "500";
      }
      if (mode !== formMode && (!isCrypto() || quote)) {
        formMode = mode;
        els.qty.value = isCrypto() ? String(quote.minQty) : "1";
        if (isCrypto() && els.leverage) {
          els.leverage.textContent = "";
          const choices = LEVERAGES.filter((value) => value <= (quote.maxLeverage || 125));
          if (!choices.includes(state.leverage)) choices.push(state.leverage);
          for (const value of choices.sort((a, b) => a - b)) {
            const option = root.document.createElement("option");
            option.value = String(value);
            option.textContent = `${value}x`;
            els.leverage.appendChild(option);
          }
        }
      }
      if (isCrypto() && els.leverage && root.document.activeElement !== els.leverage) {
        els.leverage.value = String(state.leverage);
      }
      renderAutoStops();
      renderSizingNote();
    }

    function renderButtons() {
      const quote = state && state.quote;
      const type = orderType();
      const market = type === "market";
      els.priceRow.hidden = market;
      if (els.priceLabel) els.priceLabel.textContent = type === "stop" ? "触发价" : "价格";
      const prefix = type === "stop" ? "条件" : "限价";
      els.buy.textContent = market ? `买入 ${price(quote && quote.ask)}` : `${prefix}买入`;
      els.sell.textContent = market ? `卖出 ${price(quote && quote.bid)}` : `${prefix}卖出`;
      // 市价单只在交易中可点(否则必然被拒); 限价单、条件单休市也能挂
      const blocked = submitting || !quote || (market && !quote.open);
      els.buy.disabled = blocked;
      els.sell.disabled = blocked;
      if (els.leverage) els.leverage.disabled = submitting;
      const position = currentPosition();
      els.flatten.disabled = submitting || !position || !(quote && quote.open);
      els.flatten.textContent = position ? `平仓 ${position.qty > 0 ? "多" : "空"}${Math.abs(position.qty)}` : "平仓";
      renderStopsButton();
    }

    // 两个框都清空再点就是取消; 本来就没设时不用点
    function renderStopsButton() {
      const position = currentPosition();
      const clearing = !els.tp.value && !els.sl.value;
      const cancel = clearing && !!position && (position.tp != null || position.sl != null);
      els.stopsSet.textContent = cancel ? "取消止盈止损" : "设置止盈止损";
      els.stopsSet.disabled = submitting || !position || (clearing && !cancel);
    }

    // 止盈止损编辑: 只对当前合约的持仓。服务端的值变了(刚设置、开平仓、反手)才回填输入框,
    // 每秒的轮询不能把正在敲的数字冲掉。
    function renderStops() {
      const position = currentPosition();
      els.stops.hidden = !position;
      const key = position ? JSON.stringify([state.contract, Math.sign(position.qty), position.tp, position.sl]) : "";
      if (key !== stopsKey) {
        stopsKey = key;
        els.tp.value = position && position.tp != null ? String(position.tp) : "";
        els.sl.value = position && position.sl != null ? String(position.sl) : "";
      }
      els.stopsNote.textContent = (position && position.stopError) || "";
      renderStopsButton();
    }

    function renderPosition() {
      const position = currentPosition();
      const rows = position ? [
        ["方向", `${position.qty > 0 ? "多" : "空"} ${amount(Math.abs(position.qty))} ${unit()}`,
         position.qty > 0 ? "up" : "down"],
        ["均价", formatMoney(position.avgPrice, Math.max(decimals(), 2))],
        [isCrypto() ? "标记价" : "最新", price(position.last)],
        ["浮动盈亏", signed(position.floatPnl), pnlClass(position.floatPnl)],
        ["保证金", money(position.margin)],
        ["止盈 / 止损", `${position.tp != null ? price(position.tp) : "—"} / ${position.sl != null ? price(position.sl) : "—"}`],
      ] : [["当前合约", "无持仓"]];
      if (position && (position.autoStopLoss != null || position.autoTakeProfit != null)) {
        rows.push(["目标止损 / 止盈(元)", `${money(position.autoStopLoss)} / ${money(position.autoTakeProfit)}`]);
        const pnl = (value) => value == null ? "—" : signed((value - position.avgPrice) * position.qty * position.multiplier);
        rows.push(["价位预估盈亏(元)", `${pnl(position.sl)} / ${pnl(position.tp)}`]);
      }
      if (position && isCrypto()) {
        rows.push(["杠杆", `${position.leverage}x 全仓`]);
        rows.push(["强平价(估)", position.liqPrice != null ? price(position.liqPrice) : "—"]);
      }
      // 其它合约的持仓(比如换月前的旧合约)也要看得见, 不然会忘了平
      for (const item of state.positions || []) {
        if (item.contract === state.contract) continue;
        rows.push([item.contract, `${item.qty > 0 ? "多" : "空"}${amount(Math.abs(item.qty))} ${signed(item.floatPnl)}`,
                   pnlClass(item.floatPnl)]);
      }
      grid(els.position, rows);
    }

    function renderAccount() {
      const account = state.account;
      const rows = [
        ["权益", money(account.equity)],
        ["可用", money(account.available), account.available < 0 ? "down" : ""],
        ["保证金", money(account.margin)],
        ["浮动盈亏", signed(account.floatPnl), pnlClass(account.floatPnl)],
        ["平仓盈亏", signed(account.realizedPnl), pnlClass(account.realizedPnl)],
        ["手续费", formatMoney(account.fees, 2)],
      ];
      if (isCrypto()) rows.push(["资金费", signed(account.funding), pnlClass(account.funding)]);
      grid(els.account, rows);
      els.account.title = isCrypto()
        ? `初始资金 ${money(account.initialCash)} USDT；全仓，保证金 = 名义金额 / 杠杆`
        : `初始资金 ${formatMoney(account.initialCash)}；保证金按 ${Math.round(account.marginRate * 100)}% 估算`;
    }

    function renderOrders() {
      els.orders.textContent = "";
      for (const order of state.orders || []) {
        const item = root.document.createElement("li");
        item.classList.toggle("done", order.status !== "open");
        const text = root.document.createElement("span");
        text.className = order.side === "buy" ? "up" : "down";
        text.textContent = orderLabel(order, decimals());
        const status = root.document.createElement("span");
        status.className = "trade-dim";
        status.textContent = order.status === "filled" && order.fillPrice != null
          ? `成交 ${price(order.fillPrice)}` : STATUS_LABELS[order.status] || order.status;
        status.title = [order.contract, order.createdAt, order.reason].filter(Boolean).join("  ");
        item.append(text, status);
        if (order.status === "open") {
          const cancel = root.document.createElement("button");
          cancel.className = "trade-cancel";
          cancel.textContent = "撤";
          cancel.title = "撤单";
          cancel.addEventListener("click", () => cancelOrder(order.id));
          item.append(cancel);
          // 挂单带的止盈止损另起一行(成交后才挂到持仓上); 条件单触发了却没成交的原因也另起一行
          const notes = [[stopsText(order, decimals()), "", "成交后挂到持仓上"],
                         [order.reason, " error", "价格仍在触发一侧时每轮重试"]];
          for (const [note, extra, title] of notes) {
            if (!note) continue;
            const line = root.document.createElement("span");
            line.className = "trade-order-stops-line" + extra;
            line.textContent = note;
            line.title = title;
            item.classList.add("with-stops");
            item.append(line);
          }
        }
        els.orders.appendChild(item);
      }
      if (!els.orders.children.length) els.orders.textContent = "无";
      // 委托默认折叠: 标题上标出挂单数, 折叠着也看得到还有单没成交
      const open = (state.orders || []).filter((order) => order.status === "open").length;
      els.ordersCount.textContent = open ? `挂单 ${open}` : "";
    }

    function renderTrades() {
      els.trades.textContent = "";
      for (const trade of (state.trades || []).slice(0, 20)) {
        const item = root.document.createElement("li");
        const time = root.document.createElement("span");
        time.className = "trade-dim";
        time.textContent = String(trade.at || "").slice(11);
        const text = root.document.createElement("span");
        text.className = trade.side === "buy" ? "up" : "down";
        text.textContent = `${trade.side === "buy" ? "买" : "卖"} ${trade.qty} @${FlowDataRef.formatPrice(trade.price, decimals())}`;
        const result = root.document.createElement("span");
        result.className = trade.close ? pnlClass(trade.pnl) : "trade-dim";
        result.textContent = trade.close ? signed(trade.pnl) : "开";
        item.title = `${trade.contract}  ${trade.at}  开 ${trade.open} 平 ${trade.close}  手续费 ${formatMoney(trade.fee, 2)}  成交后持仓 ${trade.position}`;
        item.append(time, text, result);
        els.trades.appendChild(item);
      }
      if (!els.trades.children.length) els.trades.textContent = "无";
    }

    function renderNote() {
      const fee = state.fee;
      if (isCrypto()) {
        const quote = state.quote || {};
        let text = `手续费: 挂单 ${(fee.maker * 100).toFixed(3)}% 吃单 ${(fee.taker * 100).toFixed(3)}%`;
        if (quote.fundingRate != null) {
          const next = quote.nextFundingTime
            ? new Date(quote.nextFundingTime).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "—";
          text += ` · 资金费率 ${(quote.fundingRate * 100).toFixed(4)}%（${next} 结算）`;
        }
        els.note.textContent = text;
        els.note.title = "模拟成交: 市价单按五档盘口逐档吃到够量, 按均价成交; 限价单挂单后要价格穿过限价才按限价成交(挂单费率)。" +
          "全仓, 权益低于维持保证金(名义金额 0.5%)按标记价强平; 到点按资金费率结算。看不到排队, 结果只作参考。";
        return;
      }
      let feeText = "手续费: 未找到费率, 按 0 计";
      if (fee) {
        feeText = fee.mode === "ratio"
          ? `手续费: 开 ${(fee.open * 1e4).toFixed(2)}‱ 平今 ${(fee.closeToday * 1e4).toFixed(2)}‱`
          : `手续费: 开 ${fee.open.toFixed(2)} 元/手 平今 ${fee.closeToday.toFixed(2)} 元/手`;
      }
      els.note.textContent = feeText;
      els.note.title = "模拟成交: 市价单按对手价整笔成交(一档量不够就拒单); 限价单挂单后要价格穿过限价才按限价成交。" +
        `报价是 500ms 快照, 看不到排队, 结果只作参考。${state.feeSource ? "费率表: " + state.feeSource : ""}`;
    }

    function render() {
      if (!state) return;
      renderQuote();
      renderButtons();
      renderPosition();
      renderStops();
      renderAccount();
      renderOrders();
      renderTrades();
      renderNote();
    }

    // 返回 {ok, text}: 成没成功和显示在面板上的那句话(图上操作拿去就地提示)
    async function send(url, init, describe) {
      submitting = true;
      renderButtons();
      try {
        const result = await fetchJson(url, init);
        pollError = false;
        const text = describe(result);
        showMessage(text, "ok");
        return { ok: true, text };
      } catch (error) {
        pollError = false;
        showMessage(error.message, "error");
        return { ok: false, text: error.message };
      } finally {
        submitting = false;
        if (state) renderButtons();
        if (polling) refreshAgain = true;
        else schedule(0);
      }
    }

    function describeOrder(order) {
      const side = order.side === "buy" ? "买" : "卖";
      const stops = stopsText(order, decimals());
      if (order.status === "filled") {
        return `已成交: ${side} ${order.qty} @${price(order.fillPrice)}` + (stops ? `，${stops} 已挂到持仓` : "");
      }
      if (order.status === "open") {
        const placed = order.type === "stop"
          ? `已挂条件单: ${side} ${order.qty}，最新价${order.side === "buy" ? "涨到" : "跌到"} ${price(order.price)} 时按市价成交`
          : `已挂单: ${side} ${order.qty} @${price(order.price)}`;
        return placed + (stops ? `，成交后挂 ${stops}` : "");
      }
      return `${STATUS_LABELS[order.status] || order.status}: ${order.reason || ""}`;
    }

    // 表单没填对: 面板上提示, 也回 {ok: false, text} 给图上的加号挂单
    function refuse(text) {
      showMessage(text, "error");
      return { ok: false, text };
    }

    // 按表单下单; 十字线加号挂单时 override 给出类型和价格({type, price}), 手数与止盈止损仍取表单里的
    async function submit(side, override) {
      if (submitting) return null;
      const size = Number(byMargin() ? els.margin.value : els.qty.value);
      if (!Number.isFinite(size) || (isCrypto() ? !(size > 0) : !Number.isInteger(size) || size < 1)) {
        return refuse(byMargin() ? "保证金金额要是有限正数" : isCrypto() ? "数量要是正数" : "手数要是正整数");
      }
      const body = { symbol: options.getSymbol(), side, [byMargin() ? "margin" : "qty"]: size,
                     type: override ? override.type : orderType(),
                     clientId: newClientId() };
      if (body.type !== "market") {
        body.price = override ? override.price : Number(els.price.value);
        if (!(body.price > 0)) return refuse(body.type === "stop" ? "条件单要填触发价" : "限价单要填价格");
      }
      if (!isCrypto() && els.autoLossEnabled && (els.autoLossEnabled.checked || els.autoProfitEnabled.checked)) {
        if (!state || state.autoStopsVersion !== AUTO_STOPS_VERSION) return refuse("请先重启服务，再使用自动止盈止损");
        for (const [enabled, input, field, label] of [
          [els.autoLossEnabled, els.autoLoss, "autoStopLoss", "止损"],
          [els.autoProfitEnabled, els.autoProfit, "autoTakeProfit", "止盈"],
        ]) {
          if (!enabled.checked) continue;
          const value = Number(input.value);
          if (!(value > 0) || !Number.isFinite(value)) return refuse(`自动${label}金额要是有限正数`);
          body[field] = value;
        }
      } else {
        const tp = readPrice(els.orderTp);
        const sl = readPrice(els.orderSl);
        if ([tp, sl].some((value) => value != null && !(value > 0))) return refuse("止盈止损价要是正数");
        if (tp != null) body.tp = tp;
        if (sl != null) body.sl = sl;
      }
      return send("/api/paper/orders", { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) },
           (order) => {
             // 价格是绝对值, 下一笔多半不再适用: 下单成功就清空, 免得带到下一笔
             els.orderTp.value = "";
             els.orderSl.value = "";
             return describeOrder(order);
           });
    }

    function submitStops() {
      const tp = readPrice(els.tp);
      const sl = readPrice(els.sl);
      if ([tp, sl].some((value) => value != null && !(value > 0))) {
        showMessage("止盈止损价要是正数", "error");
        return;
      }
      postStops(tp, sl);
    }

    // 发实际合约: 主连用标的月份, 多所汇总用币安合约。成功后先把结果写进本地数据、重画面板和图上的线,
    // 不等下一次轮询(划线松手后线不会先跳回旧价位)。
    function postStops(tp, sl) {
      const text = (value) => (value != null ? price(value) : "不设");
      return send("/api/paper/stops", { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ symbol: state.contract, tp, sl }) },
                  (result) => {
                    const position = currentPosition();
                    if (position && position.contract === result.contract) {
                      position.tp = result.tp;
                      position.sl = result.sl;
                      position.autoStopLoss = result.autoStopLoss;
                      position.autoTakeProfit = result.autoTakeProfit;
                      render();
                      onState(state);
                    }
                    return result.tp == null && result.sl == null ? "已取消止盈止损"
                      : `止盈止损已设置: 止盈 ${text(result.tp)}，止损 ${text(result.sl)}`;
                  });
    }

    // 图上划线: change 只含改动的那一项({tp: 价格} / {sl: null} 是取消), 另一项沿用持仓现在的值
    async function setStops(change) {
      const position = currentPosition();
      if (!position || submitting) return null;
      const value = (kind) => (kind in change ? change[kind] : position[kind] != null ? position[kind] : null);
      return postStops(value("tp"), value("sl"));
    }

    // 改价、撤单成功后先把服务端回的委托换进本地数据并重画, 不等下一次轮询(图上的线不会先跳回去)
    function replaceOrder(result) {
      const index = state ? (state.orders || []).findIndex((order) => order.id === result.id) : -1;
      if (index < 0) return;
      state.orders[index] = result;
      render();
      onState(state);
    }

    // 图上拖挂单线改价: 会当场成交等改不了的价位不提交(拖的时候线的标题上已经提示过), 原委托不动
    async function amendOrder(id, value) {
      const order = state && (state.orders || []).find((item) => item.id === id && item.status === "open");
      if (!order || submitting) return null;
      if (value === order.price) return null;
      const problem = amendProblem(state, order, value);
      if (problem) return refuse(`改价未提交: ${problem}，已保留原价`);
      return send(`/api/paper/orders/${encodeURIComponent(id)}`,
                  { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ price: value }) },
                  (result) => {
                    replaceOrder(result);
                    return `已改价: ${orderLabel(result, decimals())}`;
                  });
    }

    function cancelOrder(id) {
      if (submitting) return Promise.resolve(null);
      return send(`/api/paper/orders/${encodeURIComponent(id)}`, { method: "DELETE" }, (result) => {
        replaceOrder(result);
        return `已撤单: ${orderLabel(result, decimals())}`;
      });
    }

    // 十字线加号的菜单项: 按选中的价位(对齐最小变动价位)挂限价单或条件单; 没有可交易的合约时没有菜单
    function menuItems(rawPrice) {
      const quote = state && state.contract && state.quote;
      if (!quote || !Number.isFinite(rawPrice)) return [];
      const at = snapPrice(rawPrice, quote.priceTick, quote.priceDecs);
      return menuChoices(quote.last, at).map(({ side, type }) => ({
        side,
        label: `${side === "buy" ? "买入" : "卖出"}${type === "stop" ? "条件" : "限价"} ` +
          `${byMargin() ? `保证金 ${els.margin.value} USDT` : `${els.qty.value} ${unit()}`} @ ${price(at)}`,
        run: () => submit(side, { type, price: at }),
      }));
    }

    function flatten() {
      send(`/api/paper/flatten?symbol=${encodeURIComponent(options.getSymbol())}`, { method: "POST" },
           (order) => "已平仓: " + describeOrder(order).replace(/^已成交: /, ""));
    }

    function reset() {
      const crypto = isCrypto();
      const current = state ? state.account.initialCash : 1000000;
      const answer = root.prompt(`重置${crypto ? "加密" : "期货"}模拟账户: 清空全部持仓、委托与成交。\n` +
                                 `初始资金(${crypto ? "USDT" : "元"}):`, String(current));
      if (answer == null) return;
      const cash = Number(String(answer).replace(/,/g, ""));
      if (!(cash > 0)) {
        showMessage("初始资金要是正数", "error");
        return;
      }
      // 带上合约代码: 服务端据此决定重置哪一个账户
      send(`/api/paper/reset?cash=${encodeURIComponent(cash)}&symbol=${encodeURIComponent(options.getSymbol())}`,
           { method: "POST" }, () => `账户已重置, 初始资金 ${formatMoney(cash)}${crypto ? " USDT" : ""}`);
    }

    async function changeLeverage() {
      if (submitting) return;
      const value = Number(els.leverage.value);
      const symbol = options.getSymbol();
      await send(`/api/paper/leverage?symbol=${encodeURIComponent(symbol)}&leverage=${value}`,
           { method: "POST" }, () => {
             if (state && state.symbol === symbol) state.leverage = value;
             return `杠杆已改为 ${value}x`;
           });
      if (state) {
        els.leverage.value = String(state.leverage);
        renderSizingNote();
      }
    }

    els.buy.addEventListener("click", () => submit("buy"));
    els.sell.addEventListener("click", () => submit("sell"));
    els.flatten.addEventListener("click", flatten);
    els.stopsSet.addEventListener("click", submitStops);
    for (const input of [els.tp, els.sl]) {
      input.addEventListener("input", renderStopsButton);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !els.stopsSet.disabled) submitStops();
      });
    }
    els.reset.addEventListener("click", reset);
    if (els.leverage) els.leverage.addEventListener("change", changeLeverage);
    if (els.sizing && els.margin) {
      loadSizing();
      els.sizing.addEventListener("change", () => {
        saveSizing();
        if (state) renderForm();
      });
      els.margin.addEventListener("input", () => {
        saveSizing();
        renderSizingNote();
      });
    }
    if (els.autoLossEnabled) {
      loadAutoStops();
      renderAutoStops();
      els.autoLossEnabled.addEventListener("change", changeAutoStops);
      els.autoProfitEnabled.addEventListener("change", changeAutoStops);
      els.autoLoss.addEventListener("input", changeAutoStops);
      els.autoProfit.addEventListener("input", changeAutoStops);
    }
    if (els.noticeClose) els.noticeClose.addEventListener("click", () => {
      clearTimeout(noticeTimer);
      els.notice.hidden = true;
    });
    els.type.addEventListener("change", () => {
      // 切到限价、条件时用最新价预填, 省得从空白开始敲
      if (orderType() !== "market" && !els.price.value && state && state.quote && state.quote.last != null) {
        els.price.value = String(state.quote.last);
      }
      renderButtons();
      renderSizingNote();
    });
    els.toggle.addEventListener("click", () => {
      setCollapsed(!collapsed());
      schedule(0);
    });
    if (root.document) {
      root.document.addEventListener("visibilitychange", () => {
        if (root.document.hidden) clearTimeout(timer);
        else schedule(0);
      });
    }

    return {
      start() {
        setCollapsed(readCollapsed());
        renderButtons();
        schedule(0);
      },
      refresh: poll,
      setStops,
      amendOrder,
      cancelOrder,
      menuItems,
      state: () => state,
    };
  }

  const api = { snapTime, buildMarkers, priceLines, lineKey, dragTargets, dragLine, amendProblem, menuChoices, snapPrice,
                formatMoney, formatSigned, pnlClass, orderLabel, stopsText, create,
                BUY_COLOR, SELL_COLOR, TP_COLOR, SL_COLOR };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PaperPanel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
