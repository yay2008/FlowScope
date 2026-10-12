/* AI 历史档案: 只读接口, 独立的 DOM / AbortController, 不操作实时 AI 面板。
 * 列表和详情各有请求序号, 快速切换筛选、记录或关窗后不接受过期响应。存档内容只作为纯文本展示。
 */
(function (root) {
  "use strict";
  const Panel = typeof module !== "undefined" && module.exports ? require("./ai-panel.js") : root.AiPanel;
  const { tfLabel, footText, FINISH_NOTES } = Panel;
  const STATUS_LABELS = { done: "已完成", stopped: "已停止", error: "出错", unknown: "未知状态" };
  const PAGE_SIZE = 20;

  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未知"
      : new Date(date.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ");
  }

  function statusLabel(record) {
    const label = STATUS_LABELS[record.status] || STATUS_LABELS.unknown;
    return `${label}${record.finish === "length" ? " · 已截断" : ""}`;
  }

  function warningText(record) {
    const parts = [];
    if (record.status === "stopped") parts.push("这次分析已停止, 以下为当时保存的部分内容。");
    if (record.status === "error") parts.push("这次分析出错, 以下为中断前保存的内容。");
    if (record.error) parts.push(record.error);
    if (FINISH_NOTES[record.finish]) parts.push(FINISH_NOTES[record.finish]);
    return parts.join("\n");
  }

  function copyText(record) {
    return [`${record.label} (${record.symbol})`,
      `${dateLabel(record.createdAt)} 北京时间 · ${(record.periods || []).map(tfLabel).join(" + ")} · ${statusLabel(record)}`,
      warningText(record), record.answer || "这条记录没有分析正文。"].filter(Boolean).join("\n\n");
  }

  async function readJson(url, signal) {
    const response = await root.fetch(url, { signal, cache: "no-store" });
    if (!response.ok) {
      let message = `读取失败 (HTTP ${response.status})`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") message = body.detail;
      } catch (ignored) { /* 保留 HTTP 错误 */ }
      throw new Error(message);
    }
    return response.json();
  }

  function create(options) {
    const doc = root.document;
    const byId = (name) => options.byId(`ai-history-${name}`);
    const els = { panel: options.byId("ai-history") };
    for (const name of ["open", "close", "refresh", "filters", "scope", "query", "from", "to", "tf", "status",
      "content", "list", "notice", "count", "more", "detail", "back", "placeholder", "record", "record-title",
      "record-time", "copy", "copy-status", "warning", "shots", "thinking", "thinking-text", "answer", "foot"]) {
      els[name] = byId(name);
    }
    let symbol = "", items = [], total = 0, nextOffset = 0, hasMore = false, selectedId = "", record = null;
    let listController = null, detailController = null, listVersion = 0, detailVersion = 0, searchTimer = null;
    const rowButtons = new Map();

    function element(tag, className, text) {
      const node = doc.createElement(tag);
      node.className = className;
      if (text != null) node.textContent = text;
      return node;
    }

    function clearDetail() {
      detailVersion++;
      if (detailController) detailController.abort();
      detailController = null;
      selectedId = "";
      record = null;
      els.record.hidden = true;
      els.placeholder.hidden = false;
      els.placeholder.textContent = "选择一条记录查看分析详情";
      els.detail.setAttribute("aria-busy", "false");
    }

    function markSelection() {
      for (const [id, button] of rowButtons) {
        button.classList.toggle("selected", id === selectedId);
        button.setAttribute("aria-pressed", String(id === selectedId));
      }
    }

    function renderList() {
      els.list.replaceChildren();
      rowButtons.clear();
      for (const item of items) {
        const row = element("li", "");
        const button = element("button", "ai-history-item");
        button.type = "button";
        button.append(element("span", "ai-history-item-time", dateLabel(item.createdAt)),
          element("strong", "ai-history-item-title", item.label),
          element("span", "ai-history-dim", `${item.periods.map(tfLabel).join(" + ")} · ${statusLabel(item)}`),
          element("span", "ai-history-excerpt", item.excerpt || "没有分析正文"));
        button.title = item.symbol;
        button.addEventListener("click", () => select(item.id));
        row.appendChild(button);
        els.list.appendChild(row);
        rowButtons.set(item.id, button);
      }
      markSelection();
      els.more.hidden = !hasMore;
      els.count.textContent = `共 ${total} 条`;
    }

    function renderPeriods(periods) {
      const selected = els.tf.value;
      const values = new Set(periods);
      if (selected) values.add(Number(selected));
      els.tf.replaceChildren();
      const all = element("option", "", "全部周期");
      all.value = "";
      els.tf.appendChild(all);
      for (const tf of [...values].sort((a, b) => a - b)) {
        const option = element("option", "", tfLabel(tf));
        option.value = String(tf);
        els.tf.appendChild(option);
      }
      els.tf.value = selected;
    }

    function renderRecord(value) {
      els["record-title"].textContent = `${value.label} · ${value.periods.map(tfLabel).join(" + ")}`;
      els["record-time"].textContent = `${dateLabel(value.createdAt)} 北京时间 · ${value.symbol} · ${statusLabel(value)}`;
      els["copy-status"].textContent = "";
      const warning = warningText(value);
      els.warning.hidden = !warning;
      els.warning.textContent = warning;
      els.shots.replaceChildren();
      for (const chart of value.charts || []) {
        const label = Number.isInteger(chart.tf) && chart.tf > 0 ? `${tfLabel(chart.tf)} 截图` : "分析截图";
        const figure = element("figure", "ai-history-shot");
        figure.appendChild(element("figcaption", "ai-history-dim", label));
        const missing = element("span", "ai-history-missing", "截图文件缺失, 分析正文仍可查看");
        if (chart.imageUrl) {
          const button = element("button", "ai-history-image-button");
          button.type = "button";
          button.title = "点击放大 / 缩小截图";
          button.setAttribute("aria-label", `放大 / 缩小${label}`);
          button.setAttribute("aria-expanded", "false");
          const img = element("img", "");
          img.alt = label;
          img.loading = "lazy";
          img.addEventListener("error", () => { button.hidden = true; missing.hidden = false; });
          img.src = chart.imageUrl;
          button.appendChild(img);
          button.addEventListener("click", () => {
            const zoomed = figure.classList.toggle("zoom");
            button.setAttribute("aria-expanded", String(zoomed));
          });
          missing.hidden = true;
          figure.appendChild(button);
        }
        figure.appendChild(missing);
        els.shots.appendChild(figure);
      }
      els.thinking.hidden = !value.reasoning;
      els.thinking.open = false;
      els["thinking-text"].textContent = value.reasoning || "";
      els.answer.textContent = value.answer || "这条记录没有分析正文。";
      els.foot.textContent = footText(value, value);
      els.placeholder.hidden = true;
      els.record.hidden = false;
      els.detail.scrollTop = 0;
    }

    async function select(identity, reveal = true) {
      clearDetail();
      selectedId = identity;
      if (reveal) els.content.classList.add("show-detail");
      markSelection();
      els.placeholder.textContent = "正在读取分析详情…";
      els.detail.setAttribute("aria-busy", "true");
      const version = detailVersion;
      detailController = new AbortController();
      try {
        const value = await readJson(`/api/analysis/history/${encodeURIComponent(identity)}`, detailController.signal);
        if (version !== detailVersion || els.panel.hidden) return;
        record = value;
        renderRecord(value);
      } catch (error) {
        if (version !== detailVersion || els.panel.hidden || error.name === "AbortError") return;
        els.placeholder.textContent = `${error.message}。可重新点击记录重试。`;
      } finally {
        if (version === detailVersion) {
          detailController = null;
          els.detail.setAttribute("aria-busy", "false");
        }
      }
    }

    async function refresh({ append = false, keepDetail = true } = {}) {
      if (els.panel.hidden) return;
      clearTimeout(searchTimer);
      if (listController) listController.abort();
      listController = new AbortController();
      const version = ++listVersion;
      if (!keepDetail) {
        clearDetail();
        els.content.classList.remove("show-detail");
        items = [];
        total = 0;
        hasMore = false;
        renderList();
      }
      const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(append ? nextOffset : 0) });
      if (els.scope.value === "current" && symbol) query.set("symbol", symbol);
      for (const [control, parameter] of [["query", "q"], ["from", "date_from"], ["to", "date_to"], ["tf", "tf"], ["status", "status"]]) {
        if (els[control].value) query.set(parameter, els[control].value.trim());
      }
      els.notice.textContent = "正在读取历史记录…";
      els.refresh.disabled = true;
      els.more.disabled = true;
      try {
        const data = await readJson(`/api/analysis/history?${query}`, listController.signal);
        if (version !== listVersion || els.panel.hidden) return;
        items = append ? [...new Map([...items, ...data.items].map((item) => [item.id, item])).values()] : data.items;
        total = data.total;
        hasMore = data.hasMore;
        nextOffset = data.offset + data.items.length;
        renderPeriods(data.periods);
        renderList();
        const skipped = data.skipped ? `已跳过 ${data.skipped} 条无法读取的记录。` : "";
        els.notice.textContent = (items.length ? `已显示 ${items.length} / ${total} 条 · 北京时间` :
          "没有符合条件的历史分析, 可切换全部合约或调整筛选。") + (skipped ? ` ${skipped}` : "");
        if (!selectedId && items.length) await select(items[0].id, false);
      } catch (error) {
        if (version !== listVersion || els.panel.hidden || error.name === "AbortError") return;
        els.notice.textContent = `${error.message}。点击刷新重试。`;
      } finally {
        if (version === listVersion) {
          listController = null;
          els.refresh.disabled = false;
          els.more.disabled = false;
        }
      }
    }

    function open() {
      symbol = options.getSymbol();
      const label = options.getLabel ? options.getLabel() : symbol;
      els.scope.options[0].textContent = label ? `当前合约 · ${label}` : "当前合约";
      els.panel.hidden = false;
      els.open.setAttribute("aria-expanded", "true");
      els.close.focus();
      return refresh({ keepDetail: false });
    }

    function close() {
      els.panel.hidden = true;
      els.open.setAttribute("aria-expanded", "false");
      clearTimeout(searchTimer);
      listVersion++;
      if (listController) listController.abort();
      listController = null;
      clearDetail();
      els.open.focus();
    }

    async function copy() {
      if (!record) return;
      const value = copyText(record), version = detailVersion;
      try {
        if (root.navigator.clipboard && root.navigator.clipboard.writeText) {
          await root.navigator.clipboard.writeText(value);
        } else {
          const input = doc.createElement("textarea");
          input.value = value;
          input.style.position = "fixed";
          input.style.opacity = "0";
          doc.body.appendChild(input);
          try {
            input.select();
            if (!doc.execCommand("copy")) throw new Error("复制不可用");
          } finally { input.remove(); els.copy.focus(); }
        }
        if (version === detailVersion) els["copy-status"].textContent = "已复制分析全文";
      } catch (error) {
        if (version === detailVersion) els["copy-status"].textContent = "复制失败, 请选中正文手动复制";
      }
    }

    els.open.addEventListener("click", open);
    els.close.addEventListener("click", close);
    els.refresh.addEventListener("click", () => refresh());
    els.more.addEventListener("click", () => refresh({ append: true }));
    els.back.addEventListener("click", () => els.content.classList.remove("show-detail"));
    els.copy.addEventListener("click", copy);
    els.filters.addEventListener("submit", (event) => { event.preventDefault(); refresh({ keepDetail: false }); });
    for (const name of ["scope", "from", "to", "tf", "status"]) {
      els[name].addEventListener("change", () => refresh({ keepDetail: false }));
    }
    els.query.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => refresh({ keepDetail: false }), 250);
    });
    els.panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    });
    return { open, close, refresh, select };
  }

  const api = { create, dateLabel, statusLabel, warningText, copyText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AiHistory = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
