/* 模拟交易 AI 复盘：先展示服务端统计，用户确认日期和账户后才请求模型。 */
(function (root) {
  "use strict";
  const Panel = typeof module !== "undefined" && module.exports ? require("./ai-panel.js") : root.AiPanel;
  const { parseSse, footText, FINISH_NOTES } = Panel;
  const CRYPTO_SYMBOL = /^(BINANCE|OKX|AGG)\./;
  const STATUS_LABELS = { done: "已完成", stopped: "已停止", error: "出错", unknown: "未知状态" };

  function accountOf(symbol) { return CRYPTO_SYMBOL.test(symbol || "") ? "crypto" : "futures"; }
  function canReview(snapshot) {
    return !!snapshot && (Number(snapshot.summary && snapshot.summary.tradeCount) > 0 ||
      (Array.isArray(snapshot.fundings) && snapshot.fundings.length > 0));
  }
  function amount(value, currency) {
    const n = Number(value);
    return `${Number.isFinite(n) ? n.toLocaleString("zh-CN", { maximumFractionDigits: currency === "USDT" ? 6 : 2, minimumFractionDigits: 2 }) : "—"} ${currency || ""}`.trim();
  }
  function warningText(record) {
    const lines = [...(Array.isArray(record.warnings) ? record.warnings : [])];
    if (record.status === "stopped") lines.push("本次复盘已停止，以下是保存的部分内容。");
    if (record.error) lines.push(record.error);
    if (FINISH_NOTES[record.finish]) lines.push(FINISH_NOTES[record.finish]);
    return lines.join("\n");
  }
  async function readJson(url, signal) {
    const response = await root.fetch(url, { signal, cache: "no-store" });
    if (!response.ok) throw new Error(await errorDetail(response));
    return response.json();
  }
  async function errorDetail(response) {
    try {
      const body = await response.json();
      if (body && body.detail) return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (ignored) { /* HTTP 状态仍可用于定位问题 */ }
    return `HTTP ${response.status}`;
  }

  function create(options) {
    const byId = (name) => options.byId(`trade-review-${name}`);
    const names = ["open", "panel", "head", "title", "status", "close", "account", "date", "refresh", "basis", "summary",
      "contracts", "warnings", "start", "stop", "again", "history-toggle", "history", "history-refresh",
      "history-list", "history-status", "thinking", "thinking-summary", "thinking-text", "answer", "error", "foot", "body"];
    const els = Object.fromEntries(names.map((name) => [name, byId(name)]));
    const doc = root.document;
    let snapshot = null, snapshotController = null, snapshotVersion = 0;
    let runController = null, runVersion = 0, runTimer = null, startedAt = 0, phase = "";
    let historyController = null, historyVersion = 0, detailController = null, detailVersion = 0;
    let modelMeta = null, answerNode = null, thinkingNode = null;
    let drag = null, placed = false;

    function status(value) { els.status.textContent = value; }
    function clearReport() {
      modelMeta = null;
      els.thinking.hidden = true;
      els.thinking.open = false;
      els["thinking-summary"].textContent = "思考过程";
      els["thinking-text"].textContent = "";
      thinkingNode = doc.createTextNode("");
      els["thinking-text"].appendChild(thinkingNode);
      els.answer.textContent = "";
      answerNode = doc.createTextNode("");
      els.answer.appendChild(answerNode);
      els.error.textContent = "";
      els.foot.textContent = "";
    }
    function renderSnapshot(value) {
      snapshot = value;
      els.summary.replaceChildren();
      els.contracts.replaceChildren();
      const summary = value.summary || {};
      const currency = value.currency || "";
      const stats = [
        ["净收益", amount(summary.netPnl, currency)],
        ["成交笔数", String(summary.tradeCount ?? 0)],
        ["平仓胜率", summary.winRate == null ? "—" : `${Number(summary.winRate).toFixed(1)}%`],
        ["已实现盈亏", amount(summary.realizedPnl, currency)],
        ["手续费", amount(summary.fees, currency)],
        ["资金费", amount(summary.funding, currency)],
        ["平仓笔数", String(summary.closeCount ?? 0)],
        ["已实现回撤", amount(summary.maxDrawdown, currency)],
      ];
      for (const [label, text] of stats) {
        const tile = doc.createElement("div");
        tile.className = "trade-review-stat";
        const name = doc.createElement("span");
        name.textContent = label;
        const data = doc.createElement("strong");
        data.textContent = text;
        tile.append(name, data);
        els.summary.appendChild(tile);
      }
      els.basis.textContent = `${value.date || ""} ${value.dateBasis || ""} · ${currency} · ${
        (value.contracts || []).length} 个合约${value.snapshotAt ? ` · 快照 ${value.snapshotAt.replace("T", " ").replace(/\+08:00$/, " 北京时间")}` : ""}`;
      for (const item of value.contracts || []) {
        const row = doc.createElement("div");
        row.className = "trade-review-contract";
        row.textContent = `${item.contract} · ${item.tradeCount ?? 0} 笔 · 净收益 ${amount(item.netPnl, currency)}`;
        els.contracts.appendChild(row);
      }
      els.warnings.textContent = warningText(value) || (!canReview(value) ? "这一天没有成交或资金费记录。" : "");
      els.start.disabled = !canReview(value) || !!runController;
      els.again.disabled = els.start.disabled;
    }
    function clearSnapshot() {
      snapshot = null;
      els.summary.replaceChildren();
      els.contracts.replaceChildren();
      els.basis.textContent = "";
      els.warnings.textContent = "";
      els.start.disabled = true;
      els.again.disabled = true;
    }
    function setBusy(busy) {
      els.account.disabled = busy;
      els.date.disabled = busy;
      els.refresh.disabled = busy || !!snapshotController;
      els.start.disabled = busy || !canReview(snapshot);
      els.again.disabled = els.start.disabled;
      els.stop.hidden = !busy;
      els.again.hidden = busy;
      els["history-toggle"].disabled = busy;
      els["history-refresh"].disabled = busy;
      els["history-list"].querySelectorAll?.("button").forEach((button) => { button.disabled = busy; });
    }
    function stopClock() { root.clearInterval(runTimer); runTimer = null; }
    function tick() { status(`${phase} · ${Math.round((Date.now() - startedAt) / 1000)} 秒`); }
    function stop() {
      if (!runController) return;
      runVersion++;
      runController.abort();
      runController = null;
      stopClock();
      setBusy(false);
      status("已停止");
      if (els.answer.textContent) els.error.textContent = "已停止；已输出的内容由服务端保存到 data/review/。";
      refreshHistory();
    }
    function close() {
      stop();
      snapshotVersion++;
      historyVersion++;
      detailVersion++;
      snapshotController?.abort();
      historyController?.abort();
      detailController?.abort();
      els.panel.hidden = true;
      els.open.setAttribute("aria-expanded", "false");
    }
    async function loadSnapshot() {
      const version = ++snapshotVersion;
      snapshotController?.abort();
      snapshotController = new AbortController();
      els.refresh.disabled = true;
      clearSnapshot();
      clearReport();
      status("读取交易记录…");
      const params = new URLSearchParams({ account: els.account.value });
      if (els.date.value) params.set("date", els.date.value);
      try {
        const value = await readJson(`/api/review/snapshot?${params}`, snapshotController.signal);
        if (version !== snapshotVersion || els.panel.hidden) return;
        els.date.value = value.date || value.defaultDate || "";
        renderSnapshot(value);
        status(canReview(value) ? "可开始复盘" : "没有可复盘的记录");
      } catch (error) {
        if (version !== snapshotVersion || els.panel.hidden || error.name === "AbortError") return;
        status("读取失败");
        els.error.textContent = error.message;
      } finally {
        if (version === snapshotVersion) {
          snapshotController = null;
          els.refresh.disabled = !!runController;
        }
      }
    }
    function refreshSnapshot() {
      if (runController) return;
      detailVersion++;
      detailController?.abort();
      detailController = null;
      return loadSnapshot();
    }
    function append(node, value) {
      if (typeof value !== "string") return;
      const stick = els.body.scrollTop + els.body.clientHeight >= els.body.scrollHeight - 24;
      node.appendData(value);
      if (stick) els.body.scrollTop = els.body.scrollHeight;
    }
    async function run() {
      if (runController || !canReview(snapshot)) return;
      clearReport();
      const version = ++runVersion;
      const account = els.account.value, date = els.date.value;
      runController = new AbortController();
      startedAt = Date.now();
      phase = "等待 AI";
      setBusy(true);
      tick();
      runTimer = root.setInterval(tick, 1000);
      let finished = false;
      try {
        const response = await root.fetch("/api/review", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account, date }), signal: runController.signal,
        });
        if (!response.ok) throw new Error(await errorDetail(response));
        if (!response.body) throw new Error("服务端没有返回复盘数据流");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        const receive = (event) => {
          if (version !== runVersion || els.panel.hidden) return;
          const { data } = event;
          if (event.event === "meta") {
            modelMeta = data;
            if (data.snapshot && data.snapshot.account === account && data.snapshot.date === date) renderSnapshot(data.snapshot);
            else if (data.summary && data.account === account && data.date === date) renderSnapshot(data);
            els.foot.textContent = footText(modelMeta, null);
          } else if (event.event === "reasoning") {
            phase = "思考中";
            els.thinking.hidden = false;
            append(thinkingNode, data.text);
            els["thinking-summary"].textContent = `思考过程（${els["thinking-text"].textContent.length} 字）`;
          } else if (event.event === "delta") {
            phase = "输出中";
            append(answerNode, data.text);
          } else if (event.event === "done" || event.event === "error") {
            finished = true;
            stopClock();
            status(event.event === "error" ? "出错" : `完成 · ${data.elapsed ?? "—"} 秒`);
            els.error.textContent = event.event === "error" ? (data.message || "复盘失败") : (FINISH_NOTES[data.finish] || "");
            els.foot.textContent = footText(modelMeta, data);
            if (data.saved) refreshHistory();
          }
        };
        for (;;) {
          const chunk = await reader.read();
          if (version !== runVersion) return;
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const parsed = parseSse(buffer);
          buffer = parsed.rest;
          parsed.events.forEach(receive);
        }
        buffer += decoder.decode();
        parseSse(`${buffer}\n\n`).events.forEach(receive);
        if (!finished) throw new Error("连接中断，没有收到完整结果");
      } catch (error) {
        if (version !== runVersion || error.name === "AbortError") return;
        stopClock();
        status("出错");
        els.error.textContent = error.message;
      } finally {
        if (version === runVersion) {
          stopClock();
          runController = null;
          setBusy(false);
        }
      }
    }
    async function refreshHistory() {
      if (els.panel.hidden) return;
      const version = ++historyVersion;
      historyController?.abort();
      historyController = new AbortController();
      els["history-list"].replaceChildren();
      els["history-status"].textContent = "读取历史…";
      try {
        const params = new URLSearchParams({ account: els.account.value });
        const result = await readJson(`/api/review/history?${params}`, historyController.signal);
        if (version !== historyVersion || els.panel.hidden) return;
        els["history-list"].replaceChildren();
        for (const item of result.items || []) {
          const button = doc.createElement("button");
          button.type = "button";
          button.className = "trade-review-history-item";
          button.textContent = `${item.label || item.reviewDate || "复盘"} · ${STATUS_LABELS[item.status] || "未知状态"}${item.excerpt ? ` · ${item.excerpt}` : ""}`;
          button.title = item.createdAt || "";
          button.disabled = !!runController;
          button.addEventListener("click", () => showHistory(item.id));
          els["history-list"].appendChild(button);
        }
        els["history-status"].textContent = result.items?.length ? `最近 ${result.items.length} 条` : "暂无历史复盘";
      } catch (error) {
        if (version !== historyVersion || els.panel.hidden || error.name === "AbortError") return;
        els["history-status"].textContent = `读取失败：${error.message}`;
      } finally {
        if (version === historyVersion) historyController = null;
      }
    }
    async function showHistory(id) {
      if (runController) return;
      const version = ++detailVersion;
      detailController?.abort();
      detailController = new AbortController();
      snapshotVersion++;
      snapshotController?.abort();
      clearSnapshot();
      clearReport();
      status("读取历史复盘…");
      try {
        const record = await readJson(`/api/review/history/${encodeURIComponent(id)}`, detailController.signal);
        if (version !== detailVersion || els.panel.hidden) return;
        const value = record.snapshot;
        if (value) {
          els.account.value = value.account || record.account || els.account.value;
          els.date.value = value.date || record.reviewDate || "";
          renderSnapshot(value);
        }
        els.thinking.hidden = !record.reasoning;
        els["thinking-text"].textContent = record.reasoning || "";
        els.answer.textContent = record.answer || "这条记录没有复盘正文。";
        els.error.textContent = warningText(record);
        els.foot.textContent = footText(record, record);
        status(`历史复盘 · ${STATUS_LABELS[record.status] || "未知状态"}`);
        els.history.open = false;
      } catch (error) {
        if (version !== detailVersion || els.panel.hidden || error.name === "AbortError") return;
        status("读取失败");
        els.error.textContent = error.message;
      } finally {
        if (version === detailVersion) detailController = null;
      }
    }
    function open() {
      if (!els.panel.hidden) return;
      els.panel.hidden = false;
      els.open.setAttribute("aria-expanded", "true");
      if (!placed) {
        const host = els.panel.parentElement;
        els.panel.style.left = `${Math.max(8, host.clientWidth - els.panel.offsetWidth - 24)}px`;
        els.panel.style.top = "48px";
        placed = true;
      }
      els.account.value = accountOf(options.getSymbol());
      els.date.value = "";
      clearReport();
      loadSnapshot();
      refreshHistory();
    }

    els.open.addEventListener("click", open);
    els.close.addEventListener("click", close);
    els.stop.addEventListener("click", stop);
    els.start.addEventListener("click", run);
    els.again.addEventListener("click", run);
    els.refresh.addEventListener("click", refreshSnapshot);
    els.account.addEventListener("change", () => { els.date.value = ""; detailVersion++; detailController?.abort(); loadSnapshot(); refreshHistory(); });
    els.date.addEventListener("change", () => { detailVersion++; detailController?.abort(); loadSnapshot(); });
    els["history-refresh"].addEventListener("click", refreshHistory);
    els["history-toggle"].addEventListener("click", () => { els.history.open = !els.history.open; });
    els.head.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button")) return;
      drag = { x: event.clientX, y: event.clientY, left: els.panel.offsetLeft, top: els.panel.offsetTop };
      els.head.setPointerCapture(event.pointerId);
    });
    els.head.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const host = els.panel.parentElement;
      els.panel.style.left = `${Math.min(Math.max(drag.left + event.clientX - drag.x, 0), host.clientWidth - 80)}px`;
      els.panel.style.top = `${Math.min(Math.max(drag.top + event.clientY - drag.y, 0), host.clientHeight - 40)}px`;
    });
    els.head.addEventListener("pointerup", () => { drag = null; });
    els.head.addEventListener("pointercancel", () => { drag = null; });
    return { open, close, stop, run, loadSnapshot, refreshSnapshot, refreshHistory, showHistory, busy: () => !!runController };
  }
  const api = { accountOf, canReview, amount, warningText, create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TradeReview = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
