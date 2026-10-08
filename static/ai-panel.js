/* AI 看图分析(图表区上方的浮层): 截图 + 同一段行情的数值摘要 → POST /api/analyze → 流式显示 DeepSeek 的分析。
 *
 * 调模型和存盘在服务端 analysis.py, 这里只管拼请求和显示:
 * - 默认只发鼠标所在的那张图; 工具栏勾上「双图」两张一起发, 大周期在前(先定方向再找位置), 由 app.js 挑图。
 * - 截图来自图表组件的 screenshot()(画布 + 窗格名), 这里在顶上加一条标题栏(合约、周期、视图、截止时刻),
 *   长边缩到 MAX_SIDE 以内, 导出 PNG(深色底上的细线用 JPEG 会糊)。
 * - 数值表: 可视范围内的 bar, 不少于 MIN_ROWS 根(放大只看几根时往左补), 不多于 MAX_ROWS 根(取最右边);
 *   价格按合约的显示位数取整(位数未知时保留原值)、振荡值一位小数, 省 token。列名与 analysis.COLUMN_NOTES 一致。
 * - 买卖量口径按图各报各的: 取图表已加载数据的实际拆分粒度(工具栏选 15s 时 10s 图实际用 10s);
 *   切了口径、新历史还没到的图不让发。
 * - 结果流式追加。模型输出一律当纯文本(文本节点), 不进 innerHTML。
 * - 浮层标题栏可拖动、右下角可拉大小; 点「停止」或关掉浮层就中断请求, 服务端随之不再读上游,
 *   已经输出的部分照样存盘。
 * 纯函数(rowSpan / buildChartData / shotTitle / parseSse / footText)在 Node 测试里直接加载。
 */
(function (root) {
  "use strict";
  const Ind = typeof module !== "undefined" && module.exports ? require("./indicators.js") : root.FlowIndicators;

  const MIN_ROWS = 60;
  const MAX_ROWS = 120;
  const MAX_SIDE = 1600;      // 截图长边上限(像素): 模型那边会缩到约 1300×1300 的像素量, 再大也看不到
  const HEADER_PX = 30;       // 截图顶上标题栏的高度
  const BOTH_KEY = "flowscope.aiBoth";
  const COLUMNS = ["time", "open", "high", "low", "close", "volume", "buy", "sell", "delta", "cvd", "rvol",
                   ...Ind.EMA_PERIODS.map((p) => `ema${p}`), "fw", "fw_sig", "crv_slope",
                   "band_up", "band_mid", "band_dn"];
  const MODE_NAMES = { rvol: "RVOL", crvol: "CRVOL", volume: "Volume", bsv: "买卖量", delta: "Delta", cvd: "CVD" };
  const FINISH_NOTES = { length: "输出达到长度上限, 后面被截断了", content_filter: "部分内容被 DeepSeek 过滤掉了",
                         insufficient_system_resource: "DeepSeek 资源不足, 输出没有完成" };

  function tfLabel(tf) {     // 同 ChartView.tfLabel(这个文件先于 chart-view.js 加载, Node 测试里也没有它)
    if (tf % 3600 === 0) return `${tf / 3600}h`;
    if (tf % 60 === 0) return `${tf / 60}m`;
    return `${tf}s`;
  }

  // 图表时间戳(北京时间当 UTC 存) -> 「MM-DD HH:MM:SS」, 与顶栏读数同一口径
  function stamp(t) {
    return new Date(t * 1000).toISOString().slice(5, 19).replace("T", " ");
  }

  // digits 为 null(合约价格位数未知)时保留原值: 按 0 位取整会把 107.85 变成 108, 收盘和轨道的相对位置就错了
  function round(v, digits) {
    if (v == null || !Number.isFinite(v)) return null;
    return digits == null ? v : Number(v.toFixed(Math.max(0, Math.min(digits, 8))));
  }

  // 数值表取哪一段, 返回 [from, to) 下标。range 是可视的逻辑范围(就是 bar 下标, 可带小数、可越出两端)
  function rowSpan(n, range) {
    if (!n) return [0, 0];
    let from = range ? Math.max(0, Math.round(range.from)) : 0;
    let to = range ? Math.min(n - 1, Math.floor(range.to)) : n - 1;
    if (from > to) {           // 可视范围里一根都没有(拖到了空白处): 取最新的一段
      from = 0;
      to = n - 1;
    }
    if (to - from + 1 > MAX_ROWS) from = to - MAX_ROWS + 1;
    if (to - from + 1 < MIN_ROWS) from = Math.max(0, to - MIN_ROWS + 1);
    return [from, to + 1];
  }

  // 图表组件 analysisInput() 的原料 -> 请求里一张图的 {columns, rows, meta}; 还没加载完返回 null。
  // 切了买卖量口径、新历史还没到(stale)也算没加载完: 屏上和数值表还是旧口径, 发出去会和说明对不上
  function buildChartData(input) {
    const { bars, derived: d, cfg } = input;
    if (!bars || !bars.length || !d || !d.lw || !d.band) return null;
    if (input.stale || !Number.isInteger(input.ltf) || input.ltf < 0) return null;
    const pd = cfg && Number.isInteger(cfg.priceDigits) ? cfg.priceDigits : null;
    const px = (v) => round(v, pd);
    const line = (v) => round(v, pd == null ? null : pd + 1);   // 均线、轨道多留一位
    const vd = (cfg && cfg.volumeDigits) || 0;
    const [from, to] = rowSpan(bars.length, input.range);
    const rows = [];
    const coverage = {};
    for (let i = from; i < to; i++) {
      const b = bars[i];
      rows.push([b.time, px(b.open), px(b.high), px(b.low), px(b.close),
                 round(b.volume, vd), round(b.buy, vd), round(b.sell, vd), round(b.delta, vd), round(b.cvd, vd),
                 round(d.rvol[i], 2), ...d.emaLines.map((ema) => line(ema[i])),
                 round(d.lw.wave[i], 1), round(d.lw.wt2[i], 1),
                 round(d.lw.crvSlope[i], 2),
                 line(d.band.up[i]), line(d.band.mid[i]), line(d.band.dn[i])]);
      const quality = ["complete", "partial", "legacy"].includes(b.coverage) ? b.coverage : "missing";
      coverage[quality] = (coverage[quality] || 0) + 1;
    }

    let high = -Infinity, low = Infinity;
    for (const b of bars) {
      if (b.high > high) high = b.high;
      if (b.low < low) low = b.low;
    }
    const shown = input.shown || {};
    return {
      columns: COLUMNS,
      rows,
      meta: {
        view: input.view === "footprint" ? "footprint" : "candle",
        ltf: input.ltf,          // 本图买卖量实际的拆分粒度: 0 = tick 口径, 正数 = 按这么多秒的小周期 K 线归类
        shown: { ema: !!shown.ema, band: !!shown.band },
        range: { from: bars[from].time, to: bars[to - 1].time },
        loaded: { count: bars.length, from: bars[0].time, high: px(high), low: px(low) },
        coverage,
      },
    };
  }

  // 截图顶上的标题栏: 合约、周期、视图、FlowMeter 模式、截止时刻(可视范围最右那根)
  function shotTitle(context, tf, data) {
    const name = context.label && context.label !== context.symbol
      ? `${context.label}（${context.symbol}）` : context.symbol;
    const mode = MODE_NAMES[context.settings.mode] || context.settings.mode;
    return `${name}   ${tfLabel(tf)}   ${data.meta.view === "footprint" ? "足迹图" : "K线"}   FlowMeter: ${mode}   ` +
           `截至 ${stamp(data.meta.range.to)} 北京时间`;
  }

  // 截图 canvas -> 加了标题栏、缩过的 PNG data URL
  function composeShot(doc, shot, title) {
    const scale = Math.min(1, MAX_SIDE / Math.max(shot.width, shot.height));
    const width = Math.round(shot.width * scale);
    const height = Math.round(shot.height * scale);
    const out = doc.createElement("canvas");
    out.width = width;
    out.height = height + HEADER_PX;
    const ctx = out.getContext("2d");
    ctx.fillStyle = "#1e222d";
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(shot, 0, HEADER_PX, width, height);
    ctx.fillStyle = "#d1d4dc";
    ctx.font = '600 15px "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.textBaseline = "middle";
    ctx.fillText(title, 10, HEADER_PX / 2, width - 20);
    return out.toDataURL("image/png");
  }

  // SSE 文本 -> 完整的事件 [{event, data}] 与还没收完的尾巴; 注释行(心跳)和解析不了的段跳过
  function parseSse(buffer) {
    const blocks = buffer.split(/\r?\n\r?\n/);
    const rest = blocks.pop();
    const events = [];
    for (const block of blocks) {
      let event = "message";
      const data = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (!data.length) continue;
      try {
        events.push({ event, data: JSON.parse(data.join("\n")) });
      } catch (error) { /* 坏的一段跳过 */ }
    }
    return { events, rest };
  }

  // 浮层底部: 模型、token、耗时、存盘位置
  function footText(meta, result) {
    const count = (v) => Number(v || 0).toLocaleString("en-US");
    const parts = [];
    if (meta && meta.model) parts.push(`${meta.model}${meta.reasoningEffort ? ` · 思考 ${meta.reasoningEffort}` : ""}`);
    const usage = result && result.usage;
    if (usage) {
      const cached = usage.prompt_cache_hit_tokens ? `(缓存命中 ${count(usage.prompt_cache_hit_tokens)})` : "";
      parts.push(`输入 ${count(usage.prompt_tokens)}${cached} · 输出 ${count(usage.completion_tokens)} token`);
    }
    if (result && result.elapsed != null) parts.push(`${result.elapsed} 秒`);
    if (result && result.saved) parts.push(`已存 data/${result.saved}`);
    else if (result && result.saveError) parts.push(`存盘失败: ${result.saveError}`);
    return parts.join(" · ");
  }

  async function errorDetail(resp) {
    try {
      const body = await resp.json();
      if (body && body.detail) return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (ignored) { /* 非 JSON 错误响应 */ }
    return `HTTP ${resp.status}`;
  }

  /* options:
   *   byId               取页面元素
   *   collect(both)      点按钮时取原料: {symbol, label, settings, paper, sources: [{tf, input, screenshot()}]}
   *                      (sources 由页面按「双图」挑好、排好顺序; input 是图表组件的 analysisInput())
   */
  function create(options) {
    const byId = options.byId;
    const doc = root.document;
    const els = {
      run: byId("ai-run"), both: byId("ai-both"), panel: byId("ai-panel"), head: byId("ai-head"),
      title: byId("ai-title"), status: byId("ai-status"), stop: byId("ai-stop"), again: byId("ai-again"),
      close: byId("ai-close"), body: byId("ai-body"), shots: byId("ai-shots"), thinking: byId("ai-thinking"),
      thinkingSummary: byId("ai-thinking-summary"), thinkingText: byId("ai-thinking-text"), text: byId("ai-text"),
      error: byId("ai-error"), foot: byId("ai-foot"),
    };
    let controller = null;     // 进行中的请求, null = 空闲
    let timer = null;          // 状态栏的秒数
    let startedAt = 0;
    let phase = "";
    let meta = null;
    let answerNode = null, thinkingNode = null, thinkingChars = 0, answerChars = 0;
    let placed = false;        // 第一次打开时摆到图表区右上角, 之后留在用户拖到的位置
    let drag = null;

    try {
      els.both.checked = root.localStorage.getItem(BOTH_KEY) === "1";
    } catch (error) { /* 记不住不影响使用 */ }
    els.both.addEventListener("change", () => {
      try {
        root.localStorage.setItem(BOTH_KEY, els.both.checked ? "1" : "0");
      } catch (error) { /* 记不住不影响使用 */ }
    });

    function setStatus(text) {
      els.status.textContent = text;
    }

    function showBusy() {
      setStatus(`${phase} · ${Math.round((Date.now() - startedAt) / 1000)} 秒`);
    }

    // 收到 done / error 就停表: 之后流还要一小会儿才关, 不能让计时把「完成」盖回「输出中」
    function stopClock() {
      clearInterval(timer);
      timer = null;
    }

    function setBusy(busy) {
      els.run.disabled = busy;
      els.run.textContent = busy ? "分析中…" : "AI 分析";
      els.stop.hidden = !busy;
      els.again.hidden = busy;
    }

    function open() {
      els.panel.hidden = false;
      if (placed) return;
      const host = els.panel.parentElement;
      els.panel.style.left = `${Math.max(8, host.clientWidth - els.panel.offsetWidth - 72)}px`;
      els.panel.style.top = "12px";
      placed = true;
    }

    function reset() {
      meta = null;
      els.shots.textContent = "";
      els.thinking.hidden = true;
      els.thinking.open = false;
      els.thinkingSummary.textContent = "思考过程";
      els.thinkingText.textContent = "";
      thinkingNode = doc.createTextNode("");
      els.thinkingText.appendChild(thinkingNode);
      thinkingChars = 0;
      els.text.textContent = "";
      answerNode = doc.createTextNode("");
      els.text.appendChild(answerNode);
      answerChars = 0;
      els.error.textContent = "";
      els.foot.textContent = "";
    }

    // 追加文字; 看着最底下的时候跟着滚, 往上翻看的时候不打扰
    function append(node, text) {
      const body = els.body;
      const stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
      node.appendData(text);
      if (stick) body.scrollTop = body.scrollHeight;
    }

    function showShots(charts) {
      for (const chart of charts) {
        const img = doc.createElement("img");
        img.src = chart.image;
        img.alt = `${tfLabel(chart.tf)} 截图`;
        img.title = "发给 DeepSeek 的截图, 点击放大 / 缩小";
        img.addEventListener("click", () => img.classList.toggle("zoom"));
        els.shots.appendChild(img);
      }
    }

    // 一个 SSE 事件; 收到 done / error 返回 true
    function handle({ event, data }) {
      if (event === "meta") {
        meta = data;
        els.foot.textContent = footText(meta, null);
      } else if (event === "reasoning") {
        phase = "思考中";
        els.thinking.hidden = false;
        append(thinkingNode, data.text);
        thinkingChars += data.text.length;
        els.thinkingSummary.textContent = `思考过程(${thinkingChars} 字)`;
      } else if (event === "delta") {
        phase = "输出中";
        append(answerNode, data.text);
        answerChars += data.text.length;
      } else if (event === "done" || event === "error") {
        stopClock();
        if (event === "error") els.error.textContent = data.message;
        else if (FINISH_NOTES[data.finish]) els.error.textContent = FINISH_NOTES[data.finish];
        setStatus(event === "error" ? "出错" : `完成 · ${data.elapsed} 秒`);
        els.foot.textContent = footText(meta, data);
        return true;
      }
      return false;
    }

    // 点按钮时取原料、截图、拼请求; 图还没加载完抛错
    function prepare(both) {
      const context = options.collect(both);
      const charts = context.sources.map((source) => {
        const data = buildChartData(source.input);
        if (!data) throw new Error(`${tfLabel(source.tf)} 图还没加载完, 稍后再试`);
        const image = composeShot(doc, source.screenshot(), shotTitle(context, source.tf, data));
        return { tf: source.tf, image, ...data };
      });
      return {
        title: `${context.label || context.symbol} · ${charts.map((c) => tfLabel(c.tf)).join(" + ")}`,
        body: { symbol: context.symbol, label: context.label, settings: context.settings, paper: context.paper,
                charts },
      };
    }

    async function run() {
      if (controller) return;
      open();
      reset();
      let request;
      try {
        request = prepare(els.both.checked);
      } catch (error) {
        els.title.textContent = "AI 分析";
        setStatus("没有发出");
        els.error.textContent = error.message;
        return;
      }
      els.title.textContent = `AI 分析 · ${request.title}`;
      els.title.title = els.title.textContent;
      showShots(request.body.charts);
      controller = new AbortController();
      startedAt = Date.now();
      phase = "等待 DeepSeek";
      setBusy(true);
      showBusy();
      timer = setInterval(showBusy, 1000);
      let finished = false;
      try {
        const resp = await root.fetch("/api/analyze", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request.body), signal: controller.signal,
        });
        if (!resp.ok) throw new Error(await errorDetail(resp));
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSse(buffer);
          buffer = parsed.rest;
          for (const event of parsed.events) {
            if (handle(event)) finished = true;
          }
        }
        if (!finished) throw new Error("连接中断, 没有收到完整结果");
      } catch (error) {
        if (error.name === "AbortError") {
          setStatus("已停止");
          if (answerChars) els.error.textContent = "已停止; 已经输出的部分服务端照样存到 data/analysis/";
        } else {
          setStatus("出错");
          els.error.textContent = error.message;
        }
      } finally {
        stopClock();
        controller = null;
        setBusy(false);
      }
    }

    function stop() {
      if (controller) controller.abort();
    }

    function close() {
      stop();
      els.panel.hidden = true;
    }

    // 标题栏拖动(按钮除外), 不拖出图表区
    els.head.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button")) return;
      drag = { x: event.clientX, y: event.clientY, left: els.panel.offsetLeft, top: els.panel.offsetTop };
      els.head.setPointerCapture(event.pointerId);
    });
    els.head.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const host = els.panel.parentElement;
      const left = Math.min(Math.max(drag.left + event.clientX - drag.x, 0), host.clientWidth - 80);
      const top = Math.min(Math.max(drag.top + event.clientY - drag.y, 0), host.clientHeight - 40);
      els.panel.style.left = `${left}px`;
      els.panel.style.top = `${top}px`;
    });
    const endDrag = () => { drag = null; };
    els.head.addEventListener("pointerup", endDrag);
    els.head.addEventListener("pointercancel", endDrag);

    els.run.addEventListener("click", run);
    els.again.addEventListener("click", run);
    els.stop.addEventListener("click", stop);
    els.close.addEventListener("click", close);

    return { run, stop, close, busy: () => controller !== null };
  }

  const api = { COLUMNS, MIN_ROWS, MAX_ROWS, rowSpan, buildChartData, shotTitle, parseSse, footText,
                create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AiPanel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
