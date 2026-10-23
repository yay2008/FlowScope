"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

test("警报面板读后端状态、单项开关不跳转、测试通知由后端发送", async () => {
  const elements = new Map(), requests = [], timers = [], handlers = {};
  function element() {
    return { children: [], events: {}, hidden: true, classList: { toggle() {} },
      setAttribute(k,v) { this[k] = v; }, append(...nodes) { this.children.push(...nodes); },
      appendChild(n) { this.children.push(n); }, replaceChildren() { this.children = []; },
      addEventListener(k,fn) { this.events[k] = fn; } };
  }
  const state = { enabled: true, disabledSymbols: [], members: ["KQ.m@SHFE.rb"], states: {},
    runtime: { "KQ.m@SHFE.rb": { status: "monitoring", wt2: 82, name: "螺纹" } }, events: [],
    notification: { status: "ready", error: null }, running: true };
  const context = vm.createContext({
    document: { hidden: false, getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
      createElement: element, addEventListener(k,fn) { handlers[k] = fn; } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith("settings")) state.disabledSymbols = [JSON.parse(options.body).symbol];
      return { ok: true, json: async () => url.endsWith("test") ? { queued: true } : state };
    }, setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/alert-panel.js"), "utf8"), context);
  vm.runInContext("globalThis.panel = FlowAlerts.mount()", context);
  const settle = async () => { for (let i=0;i<6;i++) await Promise.resolve(); };
  await settle();
  assert.equal(elements.get("alerts-open").textContent, "WT2 警报 1/1");
  const button = context.panel.buttonFor("KQ.m@SHFE.rb");
  let stopped = false;
  button.events.click({ stopPropagation() { stopped = true; } });
  await settle();
  assert.equal(stopped, true);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { enabled: false, symbol: "KQ.m@SHFE.rb" });
  await elements.get("alerts-test").events.click();
  assert.equal(requests.at(-1).url, "/api/alerts/test");
  assert.equal(elements.get("alerts-test-result").textContent, "正在发送测试通知…");
  context.document.hidden = true;
  const previous = requests.length;
  await context.panel.refresh();
  assert.equal(requests.length, previous);
});
