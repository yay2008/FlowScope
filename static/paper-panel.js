/* 模拟交易面板(右侧): 下单、持仓、账户、委托与成交; 以及图表上的成交标记和价格线的数据。
 *
 * 后端撮合在 paper.py: 市价单按对手价整笔成交, 限价单要价格穿过才成交(看不到排队)。
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
  const POLL_MS = 1000;
  const POLL_COLLAPSED_MS = 5000;
  const COLLAPSED_KEY = "flowscope.tradeCollapsed";
  const STATUS_LABELS = { open: "挂单中", filled: "已成交", cancelled: "已撤", rejected: "已拒" };

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

  // 成交标记: 买在 K 线下方红色上箭头, 卖在上方绿色下箭头; 早于图表第一根的不画。
  function buildMarkers(trades, bars) {
    const markers = [];
    for (const trade of trades || []) {
      const time = snapTime(bars, trade.time);
      if (time == null) continue;
      const buy = trade.side === "buy";
      markers.push({ time, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
                     color: buy ? BUY_COLOR : SELL_COLOR, text: `${buy ? "买" : "卖"}${trade.qty}` });
    }
    return markers.sort((a, b) => a.time - b.time);   // 库要求标记按时间升序
  }

  // 当前合约的持仓均价(实线)与挂单价(虚线)
  function priceLines(state) {
    const contract = state && state.contract;
    if (!contract) return [];
    const lines = [];
    const position = (state.positions || []).find((item) => item.contract === contract);
    if (position) {
      const long = position.qty > 0;
      lines.push({ price: position.avgPrice, color: long ? BUY_COLOR : SELL_COLOR, style: "solid",
                   title: `${long ? "多" : "空"}${Math.abs(position.qty)} 均价` });
    }
    for (const order of state.orders || []) {
      if (order.status !== "open" || order.contract !== contract) continue;
      const buy = order.side === "buy";
      lines.push({ price: order.price, color: buy ? BUY_COLOR : SELL_COLOR, style: "dashed",
                   title: `${buy ? "买" : "卖"}${order.qty} 挂单` });
    }
    return lines;
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
    const price = order.type === "limit" ? `限 ${FlowDataRef.formatPrice(order.price, decimals)}` : "市价";
    return `${side} ${order.qty} ${price}`;
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
      buy: byId("trade-buy"), sell: byId("trade-sell"), flatten: byId("trade-flatten"), msg: byId("trade-msg"),
      position: byId("trade-position"), account: byId("trade-account"), orders: byId("trade-orders"),
      trades: byId("trade-trades"), note: byId("trade-note"), reset: byId("trade-reset"),
    };
    let state = null;
    let timer = null;
    let polling = false;
    let submitting = false;
    let pollError = false;   // 当前提示来自轮询失败: 下一次轮询成功就清掉, 下单结果的提示则保留

    const collapsed = () => els.panel.classList.contains("collapsed");
    const orderType = () => (els.type.value === "limit" ? "limit" : "market");
    const decimals = () => (state && state.quote ? state.quote.priceDecs : 0);
    const price = (value) => FlowDataRef.formatPrice(value, decimals());

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

    function schedule(delay) {
      clearTimeout(timer);
      timer = null;
      if (root.document && root.document.hidden) return;
      timer = setTimeout(poll, delay == null ? (collapsed() ? POLL_COLLAPSED_MS : POLL_MS) : delay);
    }

    async function poll() {
      timer = null;
      if (polling) return;
      polling = true;
      try {
        state = await fetchJson(`/api/paper?symbol=${encodeURIComponent(options.getSymbol())}`);
        if (pollError) { showMessage(""); pollError = false; }
        if (state.error && !submitting) { showMessage(state.error, "error"); pollError = true; }
        render();
        onState(state);
      } catch (error) {
        showMessage("模拟账户加载失败：" + error.message, "error");
        pollError = true;
      } finally {
        polling = false;
        schedule();
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
      const status = quote ? (quote.open ? "交易中" : `休市 · ${quote.reason}`) : "行情未就绪";
      els.contract.textContent = `${state.contract || state.symbol} · ${status}`;
      els.contract.title = quote ? `${quote.name}  报价时间 ${quote.datetime || "—"}` : "";
      els.contract.classList.toggle("closed", !(quote && quote.open));
      els.ask.textContent = price(quote && quote.ask);
      els.askVol.textContent = quote && quote.askVolume != null ? String(quote.askVolume) : "";
      els.bid.textContent = price(quote && quote.bid);
      els.bidVol.textContent = quote && quote.bidVolume != null ? String(quote.bidVolume) : "";
      if (quote && quote.priceTick) els.price.step = String(quote.priceTick);
    }

    function renderButtons() {
      const quote = state && state.quote;
      const market = orderType() === "market";
      els.priceRow.hidden = market;
      els.buy.textContent = market ? `买入 ${price(quote && quote.ask)}` : "限价买入";
      els.sell.textContent = market ? `卖出 ${price(quote && quote.bid)}` : "限价卖出";
      // 市价单只在交易中可点(否则必然被拒); 限价单休市也能挂
      const blocked = submitting || !quote || (market && !quote.open);
      els.buy.disabled = blocked;
      els.sell.disabled = blocked;
      const position = state && (state.positions || []).find((item) => item.contract === state.contract);
      els.flatten.disabled = submitting || !position || !(quote && quote.open);
      els.flatten.textContent = position ? `平仓 ${position.qty > 0 ? "多" : "空"}${Math.abs(position.qty)}` : "平仓";
    }

    function renderPosition() {
      const position = (state.positions || []).find((item) => item.contract === state.contract);
      const rows = position ? [
        ["方向", `${position.qty > 0 ? "多" : "空"} ${Math.abs(position.qty)} 手`, position.qty > 0 ? "up" : "down"],
        ["均价", formatMoney(position.avgPrice, Math.max(decimals(), 2))],
        ["最新", price(position.last)],
        ["浮动盈亏", formatSigned(position.floatPnl), pnlClass(position.floatPnl)],
        ["保证金", formatMoney(position.margin)],
      ] : [["当前合约", "无持仓"]];
      // 其它合约的持仓(比如换月前的旧合约)也要看得见, 不然会忘了平
      for (const item of state.positions || []) {
        if (item.contract === state.contract) continue;
        rows.push([item.contract, `${item.qty > 0 ? "多" : "空"}${Math.abs(item.qty)} ${formatSigned(item.floatPnl)}`,
                   pnlClass(item.floatPnl)]);
      }
      grid(els.position, rows);
    }

    function renderAccount() {
      const account = state.account;
      grid(els.account, [
        ["权益", formatMoney(account.equity)],
        ["可用", formatMoney(account.available), account.available < 0 ? "down" : ""],
        ["保证金", formatMoney(account.margin)],
        ["浮动盈亏", formatSigned(account.floatPnl), pnlClass(account.floatPnl)],
        ["平仓盈亏", formatSigned(account.realizedPnl), pnlClass(account.realizedPnl)],
        ["手续费", formatMoney(account.fees, 2)],
      ]);
      els.account.title = `初始资金 ${formatMoney(account.initialCash)}；保证金按 ${Math.round(account.marginRate * 100)}% 估算`;
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
        }
        els.orders.appendChild(item);
      }
      if (!els.orders.children.length) els.orders.textContent = "无";
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
        result.textContent = trade.close ? formatSigned(trade.pnl) : "开";
        item.title = `${trade.contract}  ${trade.at}  开 ${trade.open} 平 ${trade.close}  手续费 ${formatMoney(trade.fee, 2)}  成交后持仓 ${trade.position}`;
        item.append(time, text, result);
        els.trades.appendChild(item);
      }
      if (!els.trades.children.length) els.trades.textContent = "无";
    }

    function renderNote() {
      const fee = state.fee;
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
      renderAccount();
      renderOrders();
      renderTrades();
      renderNote();
    }

    async function send(url, init, describe) {
      submitting = true;
      renderButtons();
      try {
        const result = await fetchJson(url, init);
        pollError = false;
        showMessage(describe(result), "ok");
      } catch (error) {
        pollError = false;
        showMessage(error.message, "error");
      } finally {
        submitting = false;
        if (state) renderButtons();
        schedule(0);
      }
    }

    function describeOrder(order) {
      const side = order.side === "buy" ? "买" : "卖";
      if (order.status === "filled") return `已成交: ${side} ${order.qty} @${price(order.fillPrice)}`;
      if (order.status === "open") return `已挂单: ${side} ${order.qty} @${price(order.price)}`;
      return `${STATUS_LABELS[order.status] || order.status}: ${order.reason || ""}`;
    }

    function submit(side) {
      const qty = Number(els.qty.value);
      if (!Number.isInteger(qty) || qty < 1) {
        showMessage("手数要是正整数", "error");
        return;
      }
      const body = { symbol: options.getSymbol(), side, qty, type: orderType(), clientId: newClientId() };
      if (body.type === "limit") {
        body.price = Number(els.price.value);
        if (!(body.price > 0)) {
          showMessage("限价单要填价格", "error");
          return;
        }
      }
      send("/api/paper/orders", { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) }, describeOrder);
    }

    function cancelOrder(id) {
      send(`/api/paper/orders/${encodeURIComponent(id)}`, { method: "DELETE" }, () => "已撤单");
    }

    function flatten() {
      send(`/api/paper/flatten?symbol=${encodeURIComponent(options.getSymbol())}`, { method: "POST" },
           (order) => "已平仓: " + describeOrder(order).replace(/^已成交: /, ""));
    }

    function reset() {
      const current = state ? state.account.initialCash : 1000000;
      const answer = root.prompt("重置模拟账户: 清空全部持仓、委托与成交。\n初始资金(元):", String(current));
      if (answer == null) return;
      const cash = Number(String(answer).replace(/,/g, ""));
      if (!(cash > 0)) {
        showMessage("初始资金要是正数", "error");
        return;
      }
      send(`/api/paper/reset?cash=${encodeURIComponent(cash)}`, { method: "POST" },
           () => `账户已重置, 初始资金 ${formatMoney(cash)}`);
    }

    els.buy.addEventListener("click", () => submit("buy"));
    els.sell.addEventListener("click", () => submit("sell"));
    els.flatten.addEventListener("click", flatten);
    els.reset.addEventListener("click", reset);
    els.type.addEventListener("change", () => {
      // 切到限价时用最新价预填, 省得从空白开始敲
      if (orderType() === "limit" && !els.price.value && state && state.quote && state.quote.last != null) {
        els.price.value = String(state.quote.last);
      }
      renderButtons();
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
      state: () => state,
    };
  }

  const api = { snapTime, buildMarkers, priceLines, formatMoney, formatSigned, pnlClass, orderLabel, create,
                BUY_COLOR, SELL_COLOR };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PaperPanel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
