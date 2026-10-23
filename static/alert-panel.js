/* 界面只读警报状态；计算、去重和 Windows 通知均在后端。 */
(function (root) {
  "use strict";
  const STATUS = { monitoring: "监控中", warming: "预热中", unavailable: "数据不可用", disabled: "已关闭" };
  const DELIVERY = { pending: "待通知", sending: "发送中", sent: "已提交 Windows", failed: "通知失败", expired: "历史补回", unknown: "发送状态未知", cancelled: "已取消" };
  const link = (symbol) => `/?symbol=${encodeURIComponent(symbol)}&alert_tf=300`;
  function label(item) {
    if (!item) return "等待后台就绪";
    const detail = item.status !== "monitoring" && item.status !== "disabled" && item.detail;
    return `${STATUS[item.status] || item.status}${detail ? ` · ${item.detail}` : item.wt2 == null ? "" : ` · WT2 ${item.wt2.toFixed(2)}`}`;
  }
  function mount({ onChange = () => {} } = {}) {
    const doc = root.document, $ = (id) => doc.getElementById(id);
    let data = null, timer = null, busy = false;
    async function request(path, body) {
      const response = await root.fetch(path, body == null ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "请求失败");
      return value;
    }
    function render() {
      if (!data) return;
      $("alerts-enabled").checked = data.enabled;
      const active = Object.values(data.runtime || {}).filter((r) => r.status === "monitoring").length;
      $("alerts-open").textContent = `WT2 警报 ${data.enabled ? `${active}/${data.members.length}` : "已关闭"}`;
      const note = data.error || data.notification?.error || (!data.running ? "警报后台未运行" : "");
      $("alerts-status").textContent = note || (data.enabled ? "后台监控中 · 关闭网页后继续 · 超买 >80 / 超卖 <20" : "后台警报已关闭");
      $("alerts-status").classList.toggle("error", !!note);
      $("alerts-test-result").textContent = data.notification?.testedAt ? "测试通知已提交 Windows" : "";
      const list = $("alerts-monitors");
      list.replaceChildren();
      for (const symbol of data.members) {
        const row = doc.createElement("li"), a = doc.createElement("a"), status = doc.createElement("span");
        const item = data.runtime[symbol] || data.states[symbol];
        a.href = link(symbol);
        a.textContent = item?.name || data.states[symbol]?.name || symbol;
        a.title = symbol;
        status.textContent = label(item);
        status.title = item?.detail || "以五分钟已收盘 K 线确认";
        row.append(a, status, buttonFor(symbol));
        list.appendChild(row);
      }
      const history = $("alerts-history");
      history.replaceChildren();
      for (const event of (data.events || []).slice(0, 20)) {
        const row = doc.createElement("li"), a = doc.createElement("a"), detail = doc.createElement("span");
        a.href = link(event.symbol);
        a.textContent = `${event.name} · ${event.zone === "overbought" ? "超买" : "超卖"} ${event.wt2.toFixed(2)}`;
        const stamp = new Date((event.bar + 300) * 1000).toLocaleString("zh-CN", { timeZone: "UTC", hour12: false });
        detail.textContent = `${stamp} · ${DELIVERY[event.delivery] || event.delivery}`;
        detail.title = event.detail || "北京时间收盘时刻";
        row.append(a, detail);
        history.appendChild(row);
      }
      if (!data.events?.length) {
        const row = doc.createElement("li");
        row.textContent = "尚无警报。首次启用先建立状态，之后首次进入极值区才提醒。";
        history.appendChild(row);
      }
      onChange();
    }
    async function refresh() {
      clearTimeout(timer);
      if (doc.hidden) return;
      try { data = await request("/api/alerts"); render(); }
      catch (error) { $("alerts-status").textContent = `警报状态读取失败：${error.message}`; }
      finally { timer = setTimeout(refresh, 5000); }
    }
    async function configure(enabled, symbol) {
      if (busy) return;
      busy = true;
      try { data = await request("/api/alerts/settings", { enabled, ...(symbol ? { symbol } : {}) }); }
      catch (error) { $("alerts-status").textContent = error.message; }
      finally { busy = false; render(); }
    }
    function buttonFor(symbol) {
      const button = doc.createElement("button"), enabled = !!data && !data.disabledSymbols.includes(symbol);
      button.type = "button";
      button.className = "alert-bell";
      button.textContent = enabled ? "🔔" : "🔕";
      button.disabled = !data || busy;
      button.setAttribute("aria-pressed", String(enabled));
      button.setAttribute("aria-label", `${enabled ? "关闭" : "开启"}${symbol}的五分钟 WT2 警报`);
      button.title = `${label(data?.runtime?.[symbol])}${data?.enabled === false ? " · 总开关已关闭" : ""}；点击${enabled ? "关闭" : "开启"}`;
      button.addEventListener("click", (event) => { event.stopPropagation(); configure(!enabled, symbol); });
      return button;
    }
    function open(show) {
      $("alerts-panel").hidden = !show;
      $("alerts-open").setAttribute("aria-expanded", String(show));
      if (show) refresh();
    }
    $("alerts-open").addEventListener("click", () => open($("alerts-panel").hidden));
    $("alerts-close").addEventListener("click", () => open(false));
    $("alerts-enabled").addEventListener("change", () => configure($("alerts-enabled").checked));
    $("alerts-test").addEventListener("click", async () => {
      $("alerts-test").disabled = true;
      try { await request("/api/alerts/test", {}); $("alerts-test-result").textContent = "正在发送测试通知…"; }
      catch (error) { $("alerts-test-result").textContent = error.message; }
      finally { $("alerts-test").disabled = false; }
    });
    doc.addEventListener("visibilitychange", () => { clearTimeout(timer); if (!doc.hidden) refresh(); });
    refresh();
    return { buttonFor, refresh };
  }
  const api = { mount, label, link };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FlowAlerts = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
