/* 图表组件: 一张 lightweight-charts 图 + 它自己的数据加载与实时推送, 可以实例化多张。
 *
 * 每张图一个主周期 tf(10s/30s/1m/5m/15m/1h/4h), 左上角的下拉框可以切换; 各自拉 GET /api/history、
 * 各自连 WS /ws, 后端按 (symbol, tf) 各出一份快照(1 分钟及以上由 30s 合成), 所以多张图之间互不干扰。
 * 渲染: 三个 pane: K线(可叠加 WaveTrend) / Volume Suite / LSMA×CRVOL;
 * 阈值与配色按 Volume Suite (By Leviathan) 口径在前端实时计算(纯计算在 indicators.js)。
 *
 * 工具栏状态(模式/阈值/视图/WT信号/CVD口径与拆分粒度, 加上图例里切的带宽)由页面持有, 以 settings 对象按引用
 * 传进来、多张图共用; 组件只读不写, 页面改完再调对应的刷新入口(见 create 的返回值)。
 * 主图指标的显示开关不在 settings 里: 每张图左上角各有一份图例, 眼睛按钮只管本图。
 * 连接状态、顶部读数和多图联动(十字光标、可视时间范围)是页面的事, 组件通过回调报出事件、
 * 提供按时间操作的入口, 时间在不同周期之间怎么对应由组件按本图的 bar 换算。
 */
(function (root) {
  "use strict";
  const { LW, BAND, WT, EMA_PERIODS, buyOf, sellOf, deltaOf } = FlowIndicators;

  // 可选主周期(秒), 与后端 indicator.TF_OPTIONS 一致(tests/test_rollup.py 核对两边相同)
  const TF_CHOICES = [10, 30, 60, 300, 900, 3600, 14400];
  // 拆分粒度候选(秒); 本周期合法的是能整除主周期的那些, 规则同后端 indicator.ltf_options, 拿到 cfg 后以 cfg 为准
  const LTF_CHOICES = [1, 5, 10, 15, 30];

  function localLtfOptions(tf) {
    return LTF_CHOICES.filter((s) => s <= tf && tf % s === 0);
  }

  // 周期(秒) -> 显示名: 10s、1m、15m、1h、4h
  function tfLabel(tf) {
    if (tf % 3600 === 0) return `${tf / 3600}h`;
    if (tf % 60 === 0) return `${tf / 60}m`;
    return `${tf}s`;
  }
  // 加载失败后自动重试: 订阅失败是暂时的(冷却期一过后端会自动重订),
  // 所以页面不该停在"加载失败"上等用户手动刷新。
  const RETRY_DELAY_MS = 5000;
  // 最后一根右边留几根的空白(图表的 rightOffset), 联动对齐右边缘时也按它留
  const RIGHT_OFFSET = 3;
  const RETRY_MAX_ATTEMPTS = 24;
  const noop = () => {};

  // ---------- 足迹图自定义 series (lightweight-charts v5 custom series) ----------
  // 数据项: {time, levels: [[price, buy, sell], ...按价格升序]}
  // 每档一格分左右两半(左卖右买), 暖色热力底色按档量强度渐变;
  // K线轮廓(影线+柱体框)垫在格子下; POC(最大量档)白框, 对角不平衡(>=3:1)色框

  const FP = {
    imbRatio: 3,                              // 对角不平衡阈值: 买[j] vs 卖[j-1] 或 卖[j] vs 买[j+1]
    imbMinVol: 10,                            // 优势侧最小量, 防小数字噪声
    imbBuyColor: "#00e676", imbSellColor: "#f23645",
    upColor: "#f0b90d", downColor: "#ef6c00", // K线轮廓: 涨金 跌橙
    pocColor: "rgba(232, 234, 237, 0.9)",
    // 判不出方向的那部分成交量: 中性紫, 与橙系热力底和绿/红不平衡框都能区分
    unknownColor: (ratio) => `rgba(167, 139, 250, ${0.35 + 0.55 * ratio})`,
  };

  // 两个自定义 series(足迹图、FlowWave带)共用的坐标契约: 库只给 visibleRange 内的 bar 算 x
  // (区间已经包含两侧只露出一半的那根); 区间外的 x 是 NaN, 或者是上次可见时留下的旧值 ——
  // 滚动/缩放之后、下一次 setData 之前不会重算。遍历全部 bar 就会拿旧坐标在画面两侧画出残影,
  // 所以 draw 只走 visibleRange。
  class FootprintRenderer {
    constructor() {
      this._data = null;
      this._options = null;
    }
    update(data, options) {
      this._data = data;
      this._options = options;
    }
    _heat(ratio) {   // 暖色热力: 同一色系, 亮度 = 档量 / bar 最大档量
      return `rgba(245, 166, 35, ${0.08 + 0.87 * ratio})`;
    }
    draw(target, priceConverter) {
      const range = this._data?.visibleRange;
      if (!range) return;
      const { bars, barSpacing } = this._data;
      const tickSize = this._options?.tickSize;
      target.useMediaCoordinateSpace(({ context: ctx }) => {
        const cellW = Math.max(barSpacing * 0.85, 6);
        const halfW = cellW / 2;
        const showText = cellW >= 42;
        ctx.font = "9px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        for (let i = range.from; i < range.to; i++) {   // 只画可见区间, 见上方说明
          const d = bars[i].originalData;
          const levels = d.levels;
          if (!levels || !levels.length) continue;
          const x = bars[i].x - cellW / 2;
          const ys = levels.map((lv) => priceConverter(lv[0]));
          const diagonals = FlowData.diagonalVolumes(levels, tickSize);
          // 稀疏成交档位之间可能隔了多个 tick，行高只按真实最小变动价位计算。
          let rowH = 6;
          if (tickSize > 0 && ys[0] != null) {
            const y2 = priceConverter(levels[0][0] + tickSize);
            if (y2 != null && y2 !== ys[0]) rowH = Math.abs(y2 - ys[0]);
          }
          // K线轮廓: 影线(最高-最低) + 柱体框(开-收), 垫在格子下
          if (d.high != null && d.low != null) {
            const yH = priceConverter(d.high), yL = priceConverter(d.low);
            if (yH != null && yL != null) {
              const frameColor = d.close >= d.open ? FP.upColor : FP.downColor;
              ctx.strokeStyle = frameColor;
              ctx.lineWidth = 1;
              ctx.beginPath();
              ctx.moveTo(bars[i].x, yH);
              ctx.lineTo(bars[i].x, yL);
              ctx.stroke();
              const yO = priceConverter(d.open), yC = priceConverter(d.close);
              if (yO != null && yC != null) {
                ctx.lineWidth = 1.5;
                ctx.strokeRect(x, Math.min(yO, yC), cellW, Math.max(Math.abs(yC - yO), 1));
              }
            }
          }
          let maxVol = 0, pocIdx = -1;
          for (let j = 0; j < levels.length; j++) {
            const v = levels[j][1] + levels[j][2] + (levels[j][3] || 0);   // 未知量也占档位总量
            if (v > maxVol) { maxVol = v; pocIdx = j; }
          }
          if (maxVol <= 0) continue;
          for (let j = 0; j < levels.length; j++) {
            const y = ys[j];
            if (y == null) continue;
            const buy = levels[j][1], sell = levels[j][2], unknown = levels[j][3] || 0;
            const top = y - rowH / 2;
            const h = Math.max(rowH - 1, 1);
            ctx.fillStyle = this._heat(sell / maxVol);
            ctx.fillRect(x, top, halfW, h);
            ctx.fillStyle = this._heat(buy / maxVol);
            ctx.fillRect(x + halfW, top, cellW - halfW, h);
            if (unknown > 0) {
              // 判不出方向的量压在格子底部整条: 不参与左卖右买, 但必须可见
              const band = Math.max(h * 0.3, 2);
              ctx.fillStyle = FP.unknownColor(unknown / maxVol);
              ctx.fillRect(x, top + h - band, cellW, band);
            }
            if (showText && rowH >= 9) {
              ctx.fillStyle = "rgba(19, 23, 34, 0.6)";      // 左右半格分隔线
              ctx.fillRect(x + halfW, top, 1, h);
              ctx.fillStyle = "rgba(232, 234, 237, 0.92)";
              ctx.fillText(String(sell), x + halfW / 2, y);
              ctx.fillText(String(buy), x + halfW * 1.5, y);
              // 对角不平衡: 买[j] >= 3*卖[j-1] 或 卖[j] >= 3*买[j+1] (levels 按价格升序)
              const diagonal = diagonals[j];
              let imbColor = null;
              if (diagonal && d.coverage === "complete") {
                if (buy >= FP.imbRatio * Math.max(diagonal.sellBelow, 1) && buy >= FP.imbMinVol) imbColor = FP.imbBuyColor;
                else if (sell >= FP.imbRatio * Math.max(diagonal.buyAbove, 1) && sell >= FP.imbMinVol) imbColor = FP.imbSellColor;
              }
              if (imbColor) {
                ctx.strokeStyle = imbColor;
                ctx.lineWidth = 1.5;
                ctx.strokeRect(x + 0.5, top + 0.5, cellW - 1, h - 1);
              }
              if (j === pocIdx) {                            // POC 白框画内侧, 与不平衡框共存时两层都可见
                ctx.strokeStyle = FP.pocColor;
                ctx.lineWidth = 1.5;
                ctx.strokeRect(x + 2.5, top + 2.5, Math.max(cellW - 5, 1), Math.max(h - 5, 1));
              }
            }
          }
        }
      });
    }
  }

  class FootprintSeries {
    constructor() {
      this._renderer = new FootprintRenderer();
    }
    defaultOptions() { return { priceLineVisible: false, lastValueVisible: false }; }
    renderer() { return this._renderer; }
    update(data, options) { this._renderer.update(data, options); }
    priceValueBuilder(plotRow) {
      const levels = plotRow.levels;
      return [Math.min(levels[0][0], plotRow.low ?? Infinity),
              Math.max(levels[levels.length - 1][0], plotRow.high ?? -Infinity),
              plotRow.close ?? levels[levels.length - 1][0]];
    }
    isWhitespace(data) { return !data.levels || !data.levels.length; }
    destroy() {}
  }

  // ---------- FlowWave 主图叠加: 价格回归通道带(custom series) ----------
  // lightweight-charts v5 没有"两条线之间填充"的原生 series, 所以和足迹图一样走 addCustomSeries, 自己在画布上画。
  // 平时只描两条很淡的上下轨(不填底色: 中性段占大半, 铺满灰底会压暗 K 线)。中线默认不画(和 EMA21 挤在一起),
  // 图例里的「中线」开关打开时画成淡虚线, 和 EMA 的实线分得开; 开关经 series 选项 midVisible 传进来。
  // 只有 wt2 超买/超卖的 bar 才上色 —— 把中线到触发那条轨(超买上轨 / 超卖下轨)之间的半边填红/绿,
  // 并把那条轨加粗加深, 一眼看出"价格压在上轨/贴着下轨"。首次越界不再另外打点: 上色段的起点就是它。
  // 颜色按 bar 归属: 相邻两根在中点切开, 左半段归左边那根、右半段归右边那根, 所以只超买一根也有一小段颜色,
  // 最新一根(右边还没有 bar)的状态也画得出来。
  // 只在时间上真正相邻的 bar 之间连(idx 差 1), 否则会横跨休市拉出一条假带。
  class BandRenderer {
    constructor() {
      this._data = null;
      this._midVisible = false;
    }
    update(data, options) {   // options 是 series 选项, 这条带只认 midVisible
      this._data = data;
      this._midVisible = !!options?.midVisible;
    }
    _color(state, alpha) {   // 与副图配色同源: 超买红 / 超卖绿 / 中性灰
      if (state > 0) return `rgba(242, 54, 69, ${alpha})`;
      if (state < 0) return `rgba(0, 230, 118, ${alpha})`;
      return `rgba(149, 152, 161, ${alpha})`;
    }
    draw(target, priceConverter) {
      const range = this._data?.visibleRange;
      if (!range) return;
      const { bars } = this._data;
      target.useMediaCoordinateSpace(({ context: ctx }) => {
        // 只取可见区间(见 FootprintRenderer 上方的坐标契约): 区间外的旧坐标会和可见的 bar 连成大块假填充。
        // 区间两端那根本身就半露在窗格外, 带照样连到窗格边缘。
        const pts = [];
        for (let i = range.from; i < range.to; i++) {
          const d = bars[i].originalData;
          const yUp = priceConverter(d.up), yDn = priceConverter(d.dn), yMid = priceConverter(d.mid);
          pts.push(yUp == null || yDn == null || yMid == null
            ? null
            : { x: bars[i].x, yUp, yDn, yMid, state: d.state, idx: d.idx });
        }
        // 相邻两根在中点切成两段, 各按自己那根的状态分进三类; 同类的段并进同一条路径一次画完:
        // 相邻多边形之间不会有抗锯齿细缝, 半透明的轨线在接头处也不会叠深
        const quiet = [], hotUp = [], hotDn = [];   // 段 [左端, 右端], 端点 {x, yUp, yDn, yMid}
        const whole = [];                           // 不切开的相邻两根, 中线用(虚线要一笔画完, 切开会打乱虚线节奏)
        for (let i = 1; i < pts.length; i++) {
          const a = pts[i - 1], b = pts[i];
          if (!a || !b || b.idx !== a.idx + 1) continue;
          whole.push([a, b]);
          const m ={ x: (a.x + b.x) / 2, yUp: (a.yUp + b.yUp) / 2, yDn: (a.yDn + b.yDn) / 2,
                      yMid: (a.yMid + b.yMid) / 2 };
          for (const [p, q, state] of [[a, m, a.state], [m, b, b.state]]) {
            (state > 0 ? hotUp : state < 0 ? hotDn : quiet).push([p, q]);
          }
        }
        const fillHalf = (segs, edge, color) => {     // 中线到 edge 那条轨之间
          ctx.fillStyle = color;
          ctx.beginPath();
          for (const [p, q] of segs) {
            ctx.moveTo(p.x, p[edge]); ctx.lineTo(q.x, q[edge]);
            ctx.lineTo(q.x, q.yMid); ctx.lineTo(p.x, p.yMid);
            ctx.closePath();
          }
          ctx.fill();
        };
        const stroke = (parts, color, width) => {     // parts: [[段列表, 哪条轨], ...]
          ctx.strokeStyle = color;
          ctx.lineWidth = width;
          ctx.beginPath();
          for (const [segs, edge] of parts) {
            let last = null;   // 前一段的右端就是这一段的左端(同一个对象)时接着画, 拐角才有正常的折线接头
            for (const [p, q] of segs) {
              if (p !== last) ctx.moveTo(p.x, p[edge]);
              ctx.lineTo(q.x, q[edge]);
              last = q;
            }
          }
          ctx.stroke();
        };
        fillHalf(hotUp, "yUp", this._color(1, 0.16));
        fillHalf(hotDn, "yDn", this._color(-1, 0.16));
        if (this._midVisible) {
          ctx.setLineDash([4, 4]);
          stroke([[whole, "yMid"]], this._color(0, 0.45), 1);
          ctx.setLineDash([]);
        }
        stroke([[quiet, "yUp"], [quiet, "yDn"], [hotUp, "yDn"], [hotDn, "yUp"]], this._color(0, 0.28), 1);
        stroke([[hotUp, "yUp"]], this._color(1, 0.9), 1.5);
        stroke([[hotDn, "yDn"]], this._color(-1, 0.9), 1.5);
      });
    }
  }

  class BandSeries {
    constructor() {
      this._renderer = new BandRenderer();
    }
    defaultOptions() { return { priceLineVisible: false, lastValueVisible: false, midVisible: false }; }
    renderer() { return this._renderer; }
    update(data, options) { this._renderer.update(data, options); }
    priceValueBuilder(plotRow) {
      // 上下轨可能超出当根 K 线的高低点, 不返回给价格轴就会被裁掉(autoscale 用这里的值)
      const vals = [plotRow.dn, plotRow.up, plotRow.mid].filter((v) => v != null);
      return vals.length ? vals : [plotRow.close ?? 0];
    }
    isWhitespace(data) { return data.mid == null || data.up == null || data.dn == null; }
    destroy() {}
  }

  // ---------- WaveTrend 背离连线(series primitive, 挂在 K 线上, 连两个枢轴的低点/高点, 同原版) ----------
  // 背离是两个枢轴之间的一条斜线, 起点往往在别的 bar 上, custom series 的逐根数据装不下,
  // 所以走 primitive: 每次绘制按时间/数值现算坐标。起点滚出可视区时 timeToCoordinate 仍给出(屏外)坐标,
  // 斜线照样画到边缘。
  class DivergencePrimitive {
    constructor() {
      this._segs = [];
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      this._view = { renderer: () => ({ draw: (target) => this._draw(target) }) };
    }
    attached({ chart, series, requestUpdate }) {
      this._chart = chart;
      this._series = series;
      this._requestUpdate = requestUpdate;
    }
    detached() {
      this._chart = this._series = this._requestUpdate = null;
    }
    // segs: [{t1, v1, t2, v2, text, color, up}], up=true 时标签写在终点下方(看涨), 否则上方
    setSegments(segs) {
      this._segs = segs;
      if (this._requestUpdate) this._requestUpdate();
    }
    updateAllViews() {}
    paneViews() { return [this._view]; }
    _draw(target) {
      if (!this._chart || !this._segs.length) return;
      const ts = this._chart.timeScale();
      target.useMediaCoordinateSpace(({ context: ctx }) => {
        ctx.lineWidth = 1;
        ctx.font = "9px Consolas, monospace";
        ctx.textAlign = "center";
        for (const s of this._segs) {
          const x1 = ts.timeToCoordinate(s.t1), x2 = ts.timeToCoordinate(s.t2);
          const y1 = this._series.priceToCoordinate(s.v1), y2 = this._series.priceToCoordinate(s.v2);
          if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
          ctx.strokeStyle = s.color;
          ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
          ctx.fillStyle = s.color;
          ctx.textBaseline = s.up ? "top" : "bottom";
          ctx.fillText(s.text, x2, s.up ? y2 + 3 : y2 - 3);
        }
      });
    }
  }

  // ---------- 主图叠加的配色与图例 ----------

  // 主图 EMA 21/55/100/200(金/蓝/青/紫); 图例里的周期数字也按这个上色, 兼当色标
  const EMA_COLORS = ["#f0b90d", "#2962ff", "#009688", "#ab47bc"];

  // 主图左上角图例的三行: 名称(悬停看说明) + 参数 + 眼睛按钮
  const LEGEND_ITEMS = [
    { key: "ema", name: "EMA", params: true,
      title: "主图指数移动平均线, 周期 21 / 55 / 100 / 200, 按收盘价计算" },
    { key: "band", name: "FlowWave带", params: true,
      title: "FlowWave 的回归通道(不影响副图 FlowWave): 中线 = 收盘价线性回归 21 根(默认不画, 点后面的「中线」显示成淡虚线), 上下轨 = 中线 ±k 倍回归残差标准差(k 点后面的「2σ」切换)。平时只有两条淡轨线; wt2 超买(大于80)时中线到上轨之间染红、上轨加粗, 超卖(小于20)时中线到下轨之间染绿、下轨加粗, 上色段的起点就是首次越界。轨道是真实价格, 可当动态支撑压力看。" },
    { key: "wt", name: "WaveTrend", params: false,
      title: "WaveTrend(LazyBear / DGT vX)。主图上画三样: ① 交叉箭头 —— 振荡线穿越信号线(振荡值的 4 根均线)时, K 线下方绿色金叉、上方红色死叉(档位在工具栏「WT信号」); ② 背离 —— 在 K 线低点/高点之间连线(RB/HB 常规/隐藏看涨, RS/HS 常规/隐藏看跌, 枢轴要等右侧 5 根才确认); ③ ±53/±60/0 参考线 —— 按每根最近 200 根的最高/最低价通道换算成价格(±60 在通道上下沿、0 在中线), 所以随通道起伏、不是水平线, 跟着价格轴拖动缩放, 不参与价格轴自动缩放。振荡线、信号线本身不画, 原数看顶部图例的 WT。" },
  ];
  // 眼睛图标; 关掉时 .ml-item.off 让斜杠显示出来
  const EYE_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/><line class="ml-slash" x1="3" y1="3" x2="21" y2="21"/></svg>';

  // 主图叠加: WaveTrend(移植自 docs/WaveTrend.pine, LazyBear 原版 + DGT 改版; 只需 OHLC, 全部前端计算)
  // 主图上画: 交叉箭头(K 线下方/上方, 同原版标签的位置)、背离连线(K 线的低点/高点之间)、±53/±60/0 参考线。
  // 振荡线、信号线、柱按需求不画; 振荡值与信号线照常计算(交叉就是两者的穿越), 原数看顶部图例的 WT。
  // 参考线换算成价格画在 K 线的价格轴上(FlowIndicators.wtPrice), 拖动/缩放价格轴时跟着 K 线一起动:
  // 换算尺是每根最近 200 根的最高价/最低价通道, ±60 落在通道上下沿, 0 落在中线, 所以这几条线随通道起伏、
  // 不是水平线, 也就只能是逐根数据的 series 而不是价格线。原版(Middle 摆位)是在最后一根上算出一个固定映射,
  // 只画最后 200 根、新 bar 一来整段都会挪; 这里逐根换算, 全历史都有。
  // 参考线一律不参与价格轴自动缩放(autoscaleInfoProvider 返回 null): 通道沿可能来自可视区左边的 bar,
  // 参与的话放大 K 线时价格轴会被撑开。
  // 超卖线不用原版振荡线的青色 #26a69a(与 EMA100 的 #009688 几乎同色), 换成浅青; 超买线用原版信号线的红。
  const WT_COLORS = { ob: "#ef5350", os: "#4dd0e1", bull: "#16a34a", bear: "#dc2626" };
  // 两档超买(实线 60 / 点线 53)、0 轴、两档超卖(点线 -53 / 实线 -60), 线型同原版
  const WT_LEVELS = [[WT.ob1, WT_COLORS.ob, "Solid"], [WT.ob2, WT_COLORS.ob, "Dotted"], [0, "rgba(149, 152, 161, 0.5)", "Solid"],
                     [WT.os2, WT_COLORS.os, "Dotted"], [WT.os1, WT_COLORS.os, "Solid"]];

  // ---------- 多图联动用的时间换算 ----------
  // 逻辑坐标就是 bar 下标(可带小数、可越出两端)。区间内按相邻两根线性插值: 休市缺口也一样插,
  // 两张图的缺口在时间上重合, 所以对得上; 越出两端(右侧留白、左侧滚出数据)按本图周期外推。
  // 调用方保证 bars 非空。

  // bars 按 time 升序: time <= t 的最后一根的下标, 没有则 -1
  function indexAtOrBefore(bars, t) {
    let lo = 0, hi = bars.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (bars[mid].time <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function timeAtLogical(bars, tf, x) {
    const last = bars.length - 1;
    if (x <= 0) return bars[0].time + x * tf;
    if (x >= last) return bars[last].time + (x - last) * tf;
    const i = Math.floor(x);
    return bars[i].time + (x - i) * (bars[i + 1].time - bars[i].time);
  }

  function logicalAtTime(bars, tf, t) {
    const last = bars.length - 1;
    if (t <= bars[0].time) return (t - bars[0].time) / tf;
    if (t >= bars[last].time) return last + (t - bars[last].time) / tf;
    const i = indexAtOrBefore(bars, t);
    return i + (t - bars[i].time) / (bars[i + 1].time - bars[i].time);
  }

  // ---------- 测量工具 ----------
  // 每张图左上角的尺子按钮(或按住 Shift 点主图)开始测量: 第一下点起点, 移动鼠标拉出区间, 第二下定住终点,
  // 定住后再点一下图表(或按 Esc、点尺子)清掉。两端的时间吸附到最近的 bar(不越出首末根), 价格取鼠标所在价位。
  // 读数三行: 涨跌(终点相对起点, 按点击先后)、K 线根数与时长、起止时刻。根数是两端 bar 的下标差(同 TradingView,
  // 只数真实存在的 bar), 时长是两端 bar 起点之间的钟面时间, 跨了休市两者对不上是正常的。

  // 与自选面板、交易面板一致: 涨红跌绿
  const MEASURE_COLORS = {
    up: { line: "#f23645", fill: "rgba(242, 54, 69, 0.14)", text: "#ffffff" },
    down: { line: "#00e676", fill: "rgba(0, 230, 118, 0.12)", text: "#131722" },
  };
  const MEASURE_FONT = '12px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';

  // 时长(秒) -> 「40秒」「6分钟30秒」「2小时5分钟」「1天3小时」: 只留最大的两级单位, 下一级为 0 就不写
  function formatDuration(seconds) {
    const units = [[86400, "天"], [3600, "小时"], [60, "分钟"], [1, "秒"]];
    const total = Math.round(Math.abs(seconds));
    for (let k = 0; k < units.length; k++) {
      const [size, name] = units[k];
      if (total < size) continue;
      const head = `${Math.floor(total / size)}${name}`;
      const next = units[k + 1];
      const tail = next ? Math.floor((total % size) / next[0]) : 0;
      return tail ? `${head}${tail}${next[1]}` : head;
    }
    return "0秒";
  }

  // 测量读数: a 起点、b 终点, 都是 {time, price}(time 是 bar 的时间)。调用方保证 bars 非空
  function measureStats(bars, tf, a, b) {
    const change = b.price - a.price;
    return {
      count: Math.round(Math.abs(logicalAtTime(bars, tf, b.time) - logicalAtTime(bars, tf, a.time))),
      seconds: Math.abs(b.time - a.time),
      change,
      pct: a.price ? (change / a.price) * 100 : null,
    };
  }

  // 带正负号, 四舍五入成 0 时不带号(不显示「-0」)
  function signed(v, digits) {
    const text = v.toFixed(digits);
    return Number(text) > 0 ? `+${text}` : Number(text) < 0 ? text : text.replace("-", "");
  }

  // 读数框的三行。价格位数同顶栏读数; 起止时刻与时间轴同一口径(UTC 显示), 两端在同一天就只写时刻
  function measureLines(stats, a, b, tf, digits) {
    const from = Math.min(a.time, b.time), to = Math.max(a.time, b.time);
    const stamp = (t) => new Date(t * 1000).toISOString().slice(5, tf < 60 ? 19 : 16).replace("T", " ");
    const sameDay = stamp(from).slice(0, 5) === stamp(to).slice(0, 5);
    const clock = (t) => (sameDay ? stamp(t).slice(6) : stamp(t));
    return [
      `${signed(stats.change, digits)}  ${stats.pct == null ? "-" : signed(stats.pct, 2) + "%"}`,
      `${stats.count} 根 · ${formatDuration(stats.seconds)}`,
      `${clock(from)} → ${clock(to)}`,
    ];
  }

  function drawArrow(ctx, xa, ya, xb, yb) {
    ctx.beginPath(); ctx.moveTo(xa, ya); ctx.lineTo(xb, yb); ctx.stroke();
    const len = Math.hypot(xb - xa, yb - ya);
    if (len < 8) return;   // 太短就不画箭头
    const ux = (xb - xa) / len, uy = (yb - ya) / len, s = 5;
    ctx.beginPath();
    ctx.moveTo(xb - ux * s - uy * s, yb - uy * s + ux * s);
    ctx.lineTo(xb, yb);
    ctx.lineTo(xb - ux * s + uy * s, yb - uy * s - ux * s);
    ctx.stroke();
  }

  // 测量区间挂在 K 线 series 上, zOrder 为 top: 画在所有 series(EMA、参考线、足迹格子)之上。不挂窗格
  // (pane primitive): 实测窗格图元的 top 仍画在 series 之下, 读数框会被 K 线压住。series 隐藏(足迹图视图)时
  // 它的 top 图元照画。坐标每次绘制时按时间/价格现算, 跟着滚动缩放走; geometry() 返回 null 就不画。
  class MeasurePrimitive {
    constructor(geometry) {
      this._geometry = geometry;
      this._requestUpdate = null;
      this._view = { zOrder: () => "top", renderer: () => ({ draw: (target) => this._draw(target) }) };
    }
    attached({ requestUpdate }) { this._requestUpdate = requestUpdate; }
    detached() { this._requestUpdate = null; }
    refresh() {
      if (this._requestUpdate) this._requestUpdate();
    }
    updateAllViews() {}
    paneViews() { return [this._view]; }
    _draw(target) {
      const g = this._geometry();
      if (!g) return;
      const color = g.up ? MEASURE_COLORS.up : MEASURE_COLORS.down;
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const left = Math.min(g.x1, g.x2), top = Math.min(g.y1, g.y2);
        const width = Math.abs(g.x2 - g.x1), height = Math.abs(g.y2 - g.y1);
        ctx.fillStyle = color.fill;
        ctx.fillRect(left, top, width, height);
        // 区间中间一横一竖两支箭头, 都从起点那一侧指向终点
        const cx = (g.x1 + g.x2) / 2, cy = (g.y1 + g.y2) / 2;
        ctx.strokeStyle = color.line;
        ctx.lineWidth = 1;
        drawArrow(ctx, g.x1, cy, g.x2, cy);
        drawArrow(ctx, cx, g.y1, cx, g.y2);
        // 读数框贴在终点价位那一侧(涨在区间上方、跌在下方), 挤不下时收进窗格内; 区间整个滚出左右两侧就不画
        if (left + width < 0 || left > mediaSize.width) return;
        ctx.font = MEASURE_FONT;
        const pad = 6, lineH = 16;
        const boxW = Math.max(...g.lines.map((s) => ctx.measureText(s).width)) + pad * 2;
        const boxH = g.lines.length * lineH + pad;
        const bx = Math.min(Math.max(cx - boxW / 2, 4), mediaSize.width - boxW - 4);
        const by = Math.min(Math.max(g.up ? top - boxH - 6 : top + height + 6, 4), mediaSize.height - boxH - 4);
        ctx.fillStyle = color.line;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, by, boxW, boxH, 4);
        else ctx.rect(bx, by, boxW, boxH);
        ctx.fill();
        ctx.fillStyle = color.text;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        g.lines.forEach((s, k) => ctx.fillText(s, bx + boxW / 2, by + pad / 2 + lineH * (k + 0.5)));
      });
    }
  }
  // 尺子图标
  const RULER_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 17 17 3l4 4L7 21z"/><path d="M6.5 13.5l2 2M9.5 10.5l1.5 1.5M12.5 7.5l2 2M15.5 4.5l1.5 1.5"/></svg>';

  function fmt(v, digits = 0) {
    return v == null ? "-" : Number(v).toFixed(digits);
  }

  function candleOf(b) {
    return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
  }

  /* 建一张图。options:
   *   host      放图的容器, 组件在里面建自己的 .chart-view
   *   tf        初始主周期(秒), 之后由左上角的下拉框切换(setTf)
   *   symbol    合约代码(切合约是整页重载, 所以也不变)
   *   settings  页面的工具栏状态, 按引用共用, 组件只读
   *   shown     本图主图指标的初始显示开关 {ema, band, wt, bandMid}, 之后由本图图例的眼睛按钮切换
   *             (bandMid 是 FlowWave带 的中线, 由那一行的「中线」按钮切换)
   * 回调(都可省略):
   *   onStatus(ok, text)        连接状态变了
   *   onLegend(text, coverage)  十字光标所在 bar(或最新一根)的读数与覆盖标记
   *   onConfig()                拿到服务端下发的 cfg(本周期合法的拆分粒度以它为准)
   *   onShownChange()           用户点了本图图例的眼睛按钮(新状态用 shown() 取)
   *   onBandK(k)                用户点了本图图例里的带宽, k 是循环到的下一档(两张图共用, 由页面写进 settings
   *                             再调各图的 rebuildBand)
   *   onCrosshair(time, price)  本图十字光标动了(用户移动, 或光标停着时本图数据变了); 移出图表时
   *                             time 为 null, 光标不在主图窗格时 price 为 null
   *   onRangeChange()           本图的可视范围变了(用户缩放拖动, 也包括新 bar 自动右移、加载后滚到最新)
   *   onTfChange()              用户在左上角切换了本图周期(新周期用 tf 取, 本图已开始按新周期加载)
   */
  function create({ host, tf, symbol, settings, shown: initialShown, onStatus = noop, onLegend = noop,
                    onConfig = noop, onShownChange = noop, onBandK = noop, onCrosshair = noop,
                    onRangeChange = noop, onTfChange = noop }) {
    let bars = [];        // 原始 bar: {time, open, high, low, close, volume, buy, sell, delta, cvd}
    let cfg = null;       // 后端配置: mult/rellen/smalen/zlen/colors
    let derived = null;   // 派生数组(rolling sma/zscore 等)
    // 本图实际请求的拆分粒度, 协议: 0=tick, 正数=实际小周期 K线。每轮加载时按工具栏定下, 推送消息按它过滤
    let ltf = 0;
    let fpBars = [];      // 足迹 bar: {time, levels: [[price, buy, sell], ...按价格升序]}
    let fpBarSpacing = null;   // 进足迹模式前的 barSpacing, 退出时恢复
    let barRevision = -1, fpRevision = -1;
    let watchdog = null;
    let ws = null;
    let wsGeneration = 0;
    let reconnectTimer = null;
    let loadGeneration = 0;
    let loadController = null;
    let retryTimer = null;
    let retryAttempts = 0;
    const shown = { ema: true, band: false, wt: true, bandMid: false, ...initialShown };   // 本图主图指标的显示开关

    // ---------- 容器: 图表本身 + 左上角的周期下拉框和主图指标图例 ----------

    const el = document.createElement("div");
    el.className = "chart-view";
    const corner = document.createElement("div");
    corner.className = "chart-corner";
    const badge = document.createElement("select");
    badge.className = "tf-select";
    for (const value of TF_CHOICES) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = tfLabel(value);
      badge.appendChild(option);
    }
    badge.value = String(tf);
    badge.title = "主图周期：K 线、成交量、CVD 都按该周期计算，1 分钟及以上由 30s 合成（足迹图只有 10s、30s）。" +
                  "指标长度按根数固定（与 TradingView 切周期行为一致），周期越大，同样根数覆盖的时长越长。";
    badge.addEventListener("change", () => setTf(Number(badge.value)));
    // 周期下拉框右边是测量按钮(说明见文件上部「测量工具」), 量的过程中旁边提示下一步
    const measureBtn = document.createElement("button");
    measureBtn.type = "button";
    measureBtn.className = "measure-btn";
    measureBtn.innerHTML = RULER_SVG;
    measureBtn.title = "测量：点击起点、再点击终点，显示涨跌幅、K 线根数、时长和起止时刻；" +
                       "也可以按住 Shift 在主图上点击直接开始。量完再点一下图表或按 Esc 清除。";
    measureBtn.addEventListener("click", () => toggleMeasure());
    const measureHint = document.createElement("span");
    measureHint.className = "measure-hint";
    measureHint.hidden = true;
    const cornerRow = document.createElement("div");
    cornerRow.className = "chart-corner-row";
    cornerRow.append(badge, measureBtn, measureHint);
    corner.appendChild(cornerRow);
    el.appendChild(corner);
    host.appendChild(el);

    // 主图指标图例: 每行 名称 + 参数 + 眼睛按钮, 眼睛只管本图。
    // FlowWave带 的参数(带宽)是个按钮, 点一下按 BAND.kOptions 循环到下一档; 两张图共用, 交给页面去改。
    // 它后面还有「中线」开关(亮 = 显示), 和眼睛一样只管本图、记进显示开关。
    const legendEl = document.createElement("div");
    legendEl.className = "main-legend";
    const legendRows = {};
    for (const item of LEGEND_ITEMS) {
      const row = document.createElement("div");
      row.className = "ml-item";
      row.setAttribute("data-key", item.key);
      const name = document.createElement("span");
      name.className = "ml-name";
      name.title = item.title;
      name.textContent = item.name;
      row.appendChild(name);
      let params = null, mid = null;
      if (item.params) {
        params = document.createElement(item.key === "band" ? "button" : "span");
        params.className = "ml-params";
        row.appendChild(params);
      }
      if (item.key === "band") {
        params.type = "button";
        params.title = "带宽 k: 上下轨 = 中线 ±k 倍回归残差标准差。点击按 " +
          BAND.kOptions.map((k) => `${k}σ`).join(" → ") + " 循环切换, 两张图共用; 本图的带关着时不能点。" +
          "实测 800 根 fu 30s 的收盘包含率: 1.5σ 81.1% / 2σ 91.0% / 2.5σ 96.6%。越窄触碰越多、假信号也越多, 越宽越少被穿。";
        params.addEventListener("click", () => {
          const ks = BAND.kOptions;
          onBandK(ks[(ks.indexOf(settings.bandK) + 1) % ks.length]);
        });
        mid = document.createElement("button");
        mid.className = "ml-toggle";
        mid.type = "button";
        mid.textContent = "中线";
        mid.addEventListener("click", () => toggleShown("bandMid"));
        row.appendChild(mid);
      }
      const eye = document.createElement("button");
      eye.className = "ml-eye";
      eye.type = "button";
      eye.innerHTML = EYE_SVG;
      eye.addEventListener("click", () => toggleShown(item.key));
      row.appendChild(eye);
      legendEl.appendChild(row);
      legendRows[item.key] = { row, params, mid, eye };
    }
    EMA_PERIODS.forEach((p, j) => {   // EMA 的周期按各自线色列出, 兼当色标
      const span = document.createElement("span");
      span.textContent = String(p);
      span.style.color = EMA_COLORS[j];
      legendRows.ema.params.appendChild(span);
    });
    corner.appendChild(legendEl);
    renderLegend();

    // ---------- 图表初始化 ----------

    const chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: {
        background: { color: "#131722" },
        textColor: "#d1d4dc",
        panes: { separatorColor: "#2a2e39", enableResize: true },
      },
      grid: {
        vertLines: { color: "#1e222d" },
        horzLines: { color: "#1e222d" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: tf < 60, rightOffset: RIGHT_OFFSET },
      rightPriceScale: { borderColor: "#2a2e39" },
    });

    // K线配色: 涨灰白, 跌灰黑
    const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#d1d4dc", downColor: "#4a4f5c",
      wickUpColor: "#d1d4dc", wickDownColor: "#4a4f5c",
      borderVisible: false,
    }, 0);

    const emaSeries = EMA_PERIODS.map((p, j) =>
      chart.addSeries(LightweightCharts.LineSeries, {
        color: EMA_COLORS[j], lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      }, 0));
    applyEmaVisibility();

    // 主图叠加: WaveTrend 参考线 + 背离连线(说明见文件上部 WT_COLORS)
    const wtLevelLines = WT_LEVELS.map(([, color, style]) => chart.addSeries(LightweightCharts.LineSeries, {
      color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle[style], priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, autoscaleInfoProvider: () => null,
    }, 0));
    const wtDivergence = new DivergencePrimitive();
    candleSeries.attachPrimitive(wtDivergence);
    applyWtVisibility();

    // 主图可选叠加: FlowWave 回归通道带(默认隐藏, 由主图左上角图例的眼睛按钮控制)
    const bandSeries = chart.addCustomSeries(new BandSeries(), { visible: false }, 0);
    // 初始可见性由状态变量决定(而不是只靠 series 创建时的 visible:false), 否则默认值一改就会状态与画面不一致
    applyBandVisibility();

    // 足迹图 series (pane 0, 默认隐藏; 视图切到足迹图时显示, tickSize 由 /api/footprint 下发)
    const fpSeries = chart.addCustomSeries(new FootprintSeries(), { visible: false, tickSize: 1 }, 0);

    // suite pane: 直方图(主值) + 直方图(卖量, 负值) + 蜡烛(CRVOL/CVD 模式)
    const histA = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" } }, 1);
    const histB = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" } }, 1);
    const candleSuite = chart.addSeries(LightweightCharts.CandlestickSeries, { borderVisible: false }, 1);

    // pane 2: LSMA × CRVOL 共振(移植自 LSMA × CRVOL 共振 V1.pine, 只需 OHLCV, 全部前端计算)
    // wave 主线固定灰色阶梯线(原版按超买红/超卖绿着色, 按需求去掉状态色, 超买超卖仍由虚线和圆点标示)
    const lwLineOpts = { lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps, priceLineVisible: false, lastValueVisible: false };
    const lwWaveGray = chart.addSeries(LightweightCharts.LineSeries, { ...lwLineOpts, color: "#9598a1" }, 2);
    // 超买超卖压力点(wt2 越线时在 80/20 上画点)
    const lwDotOpts = { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 3, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const lwDotLow = chart.addSeries(LightweightCharts.LineSeries, { ...lwDotOpts, color: "#00e676" }, 2);
    const lwDotHigh = chart.addSeries(LightweightCharts.LineSeries, { ...lwDotOpts, color: "#f7525f" }, 2);
    const lwPulse = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, 2);

    // 超买线 80 / 分水岭 50 / 超卖线 20
    lwWaveGray.createPriceLine({ price: 80, color: "rgba(242, 54, 69, 0.5)", lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, title: "" });
    lwWaveGray.createPriceLine({ price: 50, color: "rgba(149, 152, 161, 0.5)", lineStyle: LightweightCharts.LineStyle.Dotted, lineWidth: 1, title: "" });
    lwWaveGray.createPriceLine({ price: 20, color: "rgba(102, 187, 106, 0.5)", lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, title: "" });

    // setHeight 的重分配算法依赖窗格当前像素高度, 首帧前调用会算出错误权重; setStretchFactor 纯比例语义, 时序安全
    chart.panes()[0].setStretchFactor(0.60);   // K线
    chart.panes()[1].setStretchFactor(0.20);   // Volume Suite
    chart.panes()[2].setStretchFactor(0.20);   // LSMA × CRVOL

    // 窗格左上角名称标签(series 的 title 选项会显示在右侧价格轴上, 改用绝对定位 div)
    // pane 的 DOM 要到首个绘制帧才创建, 拿不到就下一帧重试(上限 120 帧防止旧版库死循环)
    function addPaneLabel(paneIndex, text) {
      let tries = 0;
      const tryAdd = () => {
        let paneEl = null;
        try { paneEl = chart.panes()[paneIndex].getHTMLElement(); } catch (e) { return; }
        if (!paneEl) {
          if (++tries < 120) requestAnimationFrame(tryAdd);
          return;
        }
        if (getComputedStyle(paneEl).position === "static") paneEl.style.position = "relative";
        const div = document.createElement("div");
        div.className = "pane-label";
        div.textContent = text;
        paneEl.appendChild(div);
      };
      tryAdd();
    }
    addPaneLabel(1, "FlowMeter");
    addPaneLabel(2, "FlowWave");

    // 模拟交易叠加: 成交标记(买红上箭头 / 卖绿下箭头, 带「买1 / 卖1」文字)与持仓均价、挂单价格线, 挂在 K 线上。
    // 数据来自页面右侧交易面板的轮询, 由页面通过 setPaperState 交进来。
    // K 线上的标记还有 WaveTrend 交叉箭头(多绿空红, 无文字)。两路拼成一份交给同一个 markers 插件:
    // 同一根 bar 上两路都有时, 库只在同一个插件里把标记上下错开, 分成两个插件会画在同一位置互相盖住。
    const candleMarkers = LightweightCharts.createSeriesMarkers(candleSeries, []);
    let paperMarkerList = [];
    let wtMarkerList = [];
    let candleMarkerKey = "";
    let paperState = null;
    let paperLineKey = "";
    let paperLines = [];

    function applyCandleMarkers() {
      const markers = [...paperMarkerList, ...wtMarkerList].sort((a, b) => a.time - b.time);   // 库要求按时间升序
      const key = JSON.stringify(markers);
      if (key === candleMarkerKey) return;   // 成交轮询每秒一次, 内容没变就不动图表
      candleMarkers.setMarkers(markers);
      candleMarkerKey = key;
    }

    // ---------- 指标计算 ----------
    // 纯计算在 indicators.js; 这里只把图表数据(bars/cfg)和工具栏状态(threshtype/bandK)喂进去,
    // 并保留同名入口给渲染代码用。

    function deriveBand() {
      return FlowIndicators.deriveBand(bars, derived.lw.wt2, settings.bandK);
    }

    function derive() {
      derived = FlowIndicators.derive(bars, cfg, settings.bandK);
    }

    function levelOf(kind, i) {
      return FlowIndicators.levelOf(derived, cfg, settings.threshtype, kind, i);
    }

    function colorFor(up, level) {
      const c = cfg.colors;
      if (level > 0) return up ? c.upLevels[level - 1] : c.downLevels[level - 1];
      return up ? c.up : c.down;
    }

    // ---------- 渲染 ----------

    function buildSuiteData() {
      const mode = settings.mode;
      const hist = [], histSell = [], candles = [];
      const n = bars.length;
      const cumulative = mode === "crvol" ? derived.crv : mode === "cvd" ? bars.map((b) => b.cvd) : null;
      for (let i = 0; i < n; i++) {
        const b = bars[i], d = derived;
        const up = b.close > b.open;      // 与原指标一致: 十字线算跌

        if (mode === "rvol" || mode === "volume") {
          const val = mode === "rvol" ? d.rvol[i] : d.vol[i];
          if (val == null) continue;
          hist.push({ time: b.time, value: val, color: colorFor(up, levelOf("vol", i)) });
        } else if (mode === "bsv") {
          const buy = buyOf(b), sell = sellOf(b);
          if (buy == null) continue;
          hist.push({ time: b.time, value: buy, color: colorFor(true, levelOf("buy", i)) });
          histSell.push({ time: b.time, value: -(sell ?? 0), color: colorFor(false, levelOf("sell", i)) });
        } else if (mode === "delta") {
          const delta = deltaOf(b);
          if (delta == null) continue;
          const lvl = levelOf(delta > 0 ? "posd" : "negd", i);   // 原指标: delta>0 才算涨
          hist.push({ time: b.time, value: delta, color: colorFor(delta > 0, lvl) });
        } else {
          // crvol / cvd: 蜡烛图, o=前一累计值, h=l=c=当前值
          const arr = cumulative;
          const shape = mode === "cvd" ? FlowData.cvdCandle(b) :
            (arr[i] == null || i === 0 || arr[i - 1] == null ? null :
              {time: b.time, open: arr[i - 1], high: Math.max(arr[i - 1], arr[i]),
               low: Math.min(arr[i - 1], arr[i]), close: arr[i]});
          if (!shape) continue;
          const lvl = mode === "crvol" ? levelOf("vol", i)
                                      : levelOf(b.delta > 0 ? "posd" : "negd", i);
          // 原指标: CRVOL 蜡烛按 K线阴阳着色, CVD 蜡烛按 delta 正负着色
          const col = mode === "crvol" ? colorFor(up, lvl) : colorFor(b.delta > 0, lvl);
          candles.push({ ...shape, color: col, wickColor: col });
        }
      }
      return { hist, histSell, candles };
    }

    function renderSuite() {
      if (!derived) return;
      const { hist, histSell, candles } = buildSuiteData();
      histA.setData(hist);
      histB.setData(histSell);
      candleSuite.setData(candles);
    }

    // LSMA × CRVOL pane 数据: 阈值沿用 cfg.mult(与 Pine th1/2/3 默认值一致)
    // 脉冲透明度对应 Pine color.new(x, 88/55/25/0); 涨 teal 跌红, 三级放量换醒目实色
    function buildLwData() {
      const wave = [], dotLow = [], dotHigh = [], pulse = [];
      const th = cfg.mult;
      const ALPHA = [0.12, 0.45, 0.75, 1];
      const L = derived.lw;
      for (let i = 0; i < bars.length; i++) {
        const b = bars[i];
        if (L.wave[i] != null) wave.push({ time: b.time, value: L.wave[i] });
        if (L.wt2[i] != null) {
          if (L.wt2[i] < LW.os) dotLow.push({ time: b.time, value: LW.os });
          else if (L.wt2[i] > LW.ob) dotHigh.push({ time: b.time, value: LW.ob });
        }
        const rv = derived.rvol[i];
        if (rv != null) {
          const up = b.close > b.open;      // 与原指标一致: 十字线算跌
          const lvl = rv >= th[2] ? 3 : rv >= th[1] ? 2 : rv >= th[0] ? 1 : 0;
          const color = lvl === 3 ? (up ? "#00e676" : "#f23645")
            : up ? `rgba(0, 150, 136, ${ALPHA[lvl]})` : `rgba(242, 54, 69, ${ALPHA[lvl]})`;
          pulse.push({ time: b.time, value: rv * 5, color });
        }
      }
      return { wave, dotLow, dotHigh, pulse };
    }

    function renderLw() {
      const { wave, dotLow, dotHigh, pulse } = buildLwData();
      lwWaveGray.setData(wave);
      lwDotLow.setData(dotLow);
      lwDotHigh.setData(dotHigh);
      lwPulse.setData(pulse);
    }

    // 参考线数据: 从振荡值出值的那根起, 每根按当根的通道换算成价格
    function buildWtLevels() {
      const W = derived.wt;
      const levels = WT_LEVELS.map(() => []);
      for (let i = 0; i < bars.length; i++) {
        if (W.osc[i] == null) continue;
        WT_LEVELS.forEach(([v], k) => levels[k].push({ time: bars[i].time, value: FlowIndicators.wtPrice(W, i, v) }));
      }
      return levels;
    }

    // 交叉信号按「WT信号」档位过滤, 金叉打在 K 线下方、死叉打在上方(同原版); 箭头大小对应原版标签的 normal / small / tiny 三档
    function buildWtMarkers() {
      const wtSignal = settings.wtSignal;
      const minLevel = wtSignal === "strong" ? 3 : wtSignal === "all" ? 1 : Infinity;
      const SIZE = { 3: 1.4, 2: 1, 1: 0.6 };
      const markers = [];
      derived.wt.cross.forEach((c, i) => {
        const level = Math.abs(c);
        if (!c || level < minLevel) return;
        markers.push({ time: bars[i].time, position: c > 0 ? "belowBar" : "aboveBar",
                       shape: c > 0 ? "arrowUp" : "arrowDown", color: c > 0 ? WT_COLORS.bull : WT_COLORS.bear,
                       size: SIZE[level] });
      });
      return markers;
    }

    // 背离连线(K 线低点连低点、高点连高点): 常规背离实色、隐藏背离半透明, 颜色同原版(color.green / color.red)
    function buildWtDivergences() {
      const COLOR = { RB: "#4caf50", HB: "rgba(76, 175, 80, 0.5)", RS: "#f23645", HS: "rgba(242, 54, 69, 0.5)" };
      return derived.wt.divs.map((d) => ({
        t1: bars[d.from].time, v1: d.fromPrice, t2: bars[d.to].time, v2: d.toPrice,
        text: d.kind, color: COLOR[d.kind], up: d.kind === "RB" || d.kind === "HB",
      }));
    }

    // 交叉箭头和背离连线都挂在 K 线 series 上, 跟着 WaveTrend 开关走, 不能靠 series 的 visible: 隐藏时直接清空
    function renderWtMarks() {
      const on = wtShown();
      wtMarkerList = on ? buildWtMarkers() : [];
      applyCandleMarkers();
      wtDivergence.setSegments(on ? buildWtDivergences() : []);
    }

    // 与 FlowWave 带一样只在 K 线视图生效: 足迹图本身已经很密
    function wtShown() {
      return shown.wt && settings.view !== "footprint";
    }

    function applyWtVisibility() {
      const on = wtShown();
      wtLevelLines.forEach((s) => s.applyOptions({ visible: on }));
      if (derived && derived.wt) renderWtMarks();
    }

    function renderWt() {
      const levels = buildWtLevels();
      wtLevelLines.forEach((s, k) => s.setData(levels[k]));
      renderWtMarks();
    }

    function renderAll() {
      derive();
      candleSeries.setData(bars.map(candleOf));
      emaSeries.forEach((s, j) =>
        s.setData(bars.map((b, i) => ({ time: b.time, value: derived.emaLines[j][i] })).filter((p) => p.value != null)));
      renderSuite();
      renderLw();
      renderWt();
      renderBand();
      renderPaperOverlays();   // bar 集合变了(补历史/修订), 成交标记要重新对齐到 bar
      updateLegend(bars.length - 1);
    }

    // 增量更新最后一根 bar
    function updateLast() {
      derive();
      const mode = settings.mode;
      const i = bars.length - 1;
      const b = bars[i];
      candleSeries.update(candleOf(b));
      emaSeries.forEach((s, j) => {
        const v = derived.emaLines[j][i];
        if (v != null) s.update({ time: b.time, value: v });
      });

      const { hist, histSell, candles } = buildSuiteData();
      // buildSuiteData 全量重建后仅 update 末点, 避免 setData 重置视图
      const latestHist = hist[hist.length - 1];
      histA.update(latestHist?.time === b.time ? latestHist : { time: b.time });
      if (mode === "bsv") {
        const latestSell = histSell[histSell.length - 1];
        histB.update(latestSell?.time === b.time ? latestSell : { time: b.time });
      }
      if (mode === "crvol" || mode === "cvd") {
        const latestCandle = candles[candles.length - 1];
        candleSuite.update(latestCandle?.time === b.time ? latestCandle : { time: b.time });
      }
      renderLw();            // 整体 setData(数据量小)
      renderWt();            // 同上
      renderBand();          // 同上: 回归通道只影响末尾若干根, 但一样整体重建最省心
      updateLegend(i);
    }

    // ---------- FlowWave 主图叠加: 数据与开关 ----------

    function buildBandData() {
      const { mid, up, dn, state } = derived.band;
      const out = [];
      for (let i = 0; i < bars.length; i++) {
        if (mid[i] == null || up[i] == null || dn[i] == null) continue;
        // idx 用来判断两根在时间上是否相邻: custom series 会把缺口两端的 bar 排在一起, 直接连会画出假带
        out.push({ time: bars[i].time, mid: mid[i], up: up[i], dn: dn[i], state: state[i],
                   close: bars[i].close, idx: i });
      }
      return out;
    }

    function renderBand() {
      if (!derived || !derived.band) return;
      bandSeries.setData(buildBandData());
    }

    // 叠加只在 K 线视图生效: 足迹图本身已经很密, 再叠带会糊成一片; 切回 K 线按开关恢复。
    function bandShown() {
      return shown.band && settings.view !== "footprint";
    }

    function applyBandVisibility() {   // 中线开关随带一起交给 series(带隐藏时中线自然也不画)
      bandSeries.applyOptions({ visible: bandShown(), midVisible: shown.bandMid });
    }

    function emaShown() {
      return shown.ema && settings.view !== "footprint";
    }

    function applyEmaVisibility() {
      const on = emaShown();
      emaSeries.forEach((s) => s.applyOptions({ visible: on }));
    }

    const APPLY_SHOWN = { ema: applyEmaVisibility, band: applyBandVisibility, wt: applyWtVisibility,
                          bandMid: applyBandVisibility };

    // ---------- 图例 ----------

    function updateLegend(i) {
      if (i < 0 || i >= bars.length) return;
      const mode = settings.mode;
      const isFp = settings.view === "footprint";
      const b = bars[i];
      const d = derived;
      const quality = isFp ? fpBars.find((fp) => fp.time === b.time)?.coverage : b.coverage;
      const coverage = "覆盖:" + (({complete: "完整", partial: "部分", missing: "缺失", legacy: "旧历史"})[quality] || "缺失");
      const t = new Date(b.time * 1000).toISOString().slice(5, 19).replace("T", " ");
      // 价格与成交量的显示位数由服务端按合约下发: 期货都是整数手, 币安 BTC 的量是小数(0.001 步长)
      const px = (v) => fmt(v, cfg?.priceDigits ?? 0);
      const vol = (v) => fmt(v, cfg?.volumeDigits ?? 0);
      const suiteVal =
        mode === "rvol" ? fmt(d.rvol[i], 2) :
        mode === "crvol" ? fmt(d.crv[i], 2) :
        mode === "volume" ? vol(b.volume) :
        mode === "bsv" ? `${vol(buyOf(b))}/${vol(sellOf(b))}` :
        mode === "delta" ? vol(deltaOf(b)) : vol(b.cvd);
      // 判向对照: 同一根 bar 同时给出新算法(买/卖/未知)与旧算法(买/卖)
      const fp = isFp ? fpBars.find((item) => item.time === b.time) : null;
      const unknown = fp ? fp.levels.reduce((sum, lv) => sum + (lv[3] || 0), 0) : (b.unknown ?? 0);
      // FlowWave带 的轨道值不进读数(这一行已经很长, 轨道看价格轴即可);
      // WaveTrend 叠加轴不显示刻度, 数值只能从这里读
      const wt = wtShown() && derived.wt.osc[i] != null
        ? `  WT:${fmt(derived.wt.osc[i], 1)}/${fmt(derived.wt.sig[i], 1)}` : "";
      const text =
        `${t}  O:${px(b.open)} H:${px(b.high)} L:${px(b.low)} C:${px(b.close)}  ` +
      `  ${mode.toUpperCase()}:${suiteVal}  Δ:${vol(deltaOf(b))}  CVD:${vol(b.cvd)}` +
      `  新买/卖:${vol(b.buy)}/${vol(b.sell)} 未知:${vol(unknown)} 旧买/卖:${vol(b.buyLegacy)}/${vol(b.sellLegacy)}` +
      `  LSMA:${fmt(derived.lw.wave[i], 1)} RVOL:${fmt(d.rvol[i], 2)} 斜率:${fmt(derived.lw.crvSlope[i], 2)}${wt}`;
      onLegend(text, coverage);
    }

    // ---------- 测量工具(交互说明见文件上部) ----------

    // off: 没有测量 / armed: 等点起点 / drawing: 起点已定, 终点跟着鼠标 / done: 两端都定了, 留在图上
    let measurePhase = "off";
    let measureA = null, measureB = null;   // 起点、终点 {time, price}
    const MEASURE_HINTS = { armed: "点击起点 · Esc 取消", drawing: "点击终点 · Esc 取消" };
    const measurePrimitive = new MeasurePrimitive(measureGeometry);
    candleSeries.attachPrimitive(measurePrimitive);

    function setMeasurePhase(phase) {
      measurePhase = phase;
      if (phase === "off" || phase === "armed") measureA = measureB = null;
      measureBtn.classList.toggle("on", phase === "armed" || phase === "drawing");
      measureHint.textContent = MEASURE_HINTS[phase] || "";
      measureHint.hidden = !MEASURE_HINTS[phase];
      measurePrimitive.refresh();
    }

    // 正在量(等起点 / 拉区间)时点尺子是取消, 否则(没在量、已量完)是开始新的一次
    function toggleMeasure() {
      setMeasurePhase(measurePhase === "armed" || measurePhase === "drawing" ? "off" : "armed");
    }

    // 鼠标事件 -> 测量端点: 时间吸附到最近的 bar(不越出首末根); 价格取鼠标所在价位,
    // 鼠标不在主图窗格时价格没有意义, 用 fallbackPrice(拉区间时就是终点原来的价位, 只动时间)
    function measurePoint(param, fallbackPrice = null) {
      if (!bars.length || param.logical == null || !param.point) return null;
      const i = Math.min(Math.max(Math.round(param.logical), 0), bars.length - 1);
      const price = param.paneIndex === 0 ? candleSeries.coordinateToPrice(param.point.y) : fallbackPrice;
      return price == null ? null : { time: bars[i].time, price };
    }

    // 每次绘制现算: 两端按时间换成逻辑坐标(端点被裁出数据窗口时按周期外推), 读数跟着当前 bars 走
    function measureGeometry() {
      if (!measureA || !measureB || !bars.length) return null;
      const ts = chart.timeScale();
      const x1 = ts.logicalToCoordinate(logicalAtTime(bars, tf, measureA.time));
      const x2 = ts.logicalToCoordinate(logicalAtTime(bars, tf, measureB.time));
      const y1 = candleSeries.priceToCoordinate(measureA.price), y2 = candleSeries.priceToCoordinate(measureB.price);
      if (x1 == null || x2 == null || y1 == null || y2 == null) return null;
      const stats = measureStats(bars, tf, measureA, measureB);
      return { x1, y1, x2, y2, up: stats.change >= 0,
               lines: measureLines(stats, measureA, measureB, tf, cfg?.priceDigits ?? 0) };
    }

    // param 是十字光标事件的参数({logical, point, paneIndex}), 鼠标不在窗格里时是 null
    function onMeasureClick(param, shiftKey) {
      if (measurePhase === "drawing") {
        measureB = (param && measurePoint(param, measureB.price)) || measureB;
        setMeasurePhase("done");
      } else if (measurePhase === "armed" || shiftKey) {
        const point = param && param.paneIndex === 0 ? measurePoint(param) : null;   // 起点只认主图窗格
        if (!point) return;
        measureA = measureB = point;
        setMeasurePhase("drawing");
      } else if (measurePhase === "done") {
        setMeasurePhase("off");
      }
    }

    // 点击自己用 DOM 事件判, 不用 chart.subscribeClick: 库把间隔很短(约 300ms 内)的两下当成双击的前半截,
    // 第二下不报, 快速量一小段时终点会点不住。位置取十字光标最近一次报的(按下之前鼠标一定先移到了那里);
    // 按下后挪动超过几像素是拖动(平移、拉窗格), 不算点击; 左上角的按钮和图例不归这里管。
    let lastPointer = null;
    let pressAt = null;
    el.addEventListener("pointerdown", (event) => {
      pressAt = event.button === 0 && !event.target.closest(".chart-corner")
        ? { x: event.clientX, y: event.clientY } : null;
    });
    el.addEventListener("pointerup", (event) => {
      const press = pressAt;
      pressAt = null;
      if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) <= 4) {
        onMeasureClick(lastPointer, event.shiftKey);
      }
    });

    // 用户移动光标时触发; 光标停着(包括 setCrosshairPosition 摆上去的)而本图数据变了, 库也会再报一次。
    // setCrosshairPosition 本身不触发。
    chart.subscribeCrosshairMove((param) => {
      lastPointer = param.point ? param : null;
      if (measurePhase === "drawing") {
        const point = measurePoint(param, measureB.price);
        if (point) {
          measureB = point;
          measurePrimitive.refresh();
        }
      }
      const price = param.time != null && param.point && param.paneIndex === 0
        ? candleSeries.coordinateToPrice(param.point.y) : null;
      onCrosshair(param.time ?? null, price);
      if (!param.time || !bars.length) { updateLegend(bars.length - 1); return; }
      const ans = indexAtOrBefore(bars, param.time);
      updateLegend(ans < 0 ? 0 : ans);
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => onRangeChange());

    // ---------- 主图指标图例 ----------

    function renderLegend() {
      legendEl.hidden = settings.view === "footprint";   // 足迹图下三个指标都强制隐藏, 图例也收起
      legendRows.band.params.textContent = `${settings.bandK}σ`;
      legendRows.band.params.disabled = !shown.band;   // 带关着时调宽度看不到效果; 足迹图下图例整个收起, 不用另管
      const mid = legendRows.band.mid;                 // 中线开关同理, 开关状态本身照旧保留
      mid.disabled = !shown.band;
      mid.classList.toggle("on", shown.bandMid);
      mid.title = "中线 = 收盘价线性回归 21 根, 画成淡虚线(和 EMA 的实线分得开), 只管本图。" +
                  (shown.bandMid ? "现在显示, 点击隐藏" : "现在隐藏, 点击显示");
      for (const key of Object.keys(legendRows)) {
        legendRows[key].row.classList.toggle("off", !shown[key]);
        legendRows[key].eye.title = shown[key] ? "隐藏" : "显示";
      }
    }

    function toggleShown(key) {
      shown[key] = !shown[key];
      APPLY_SHOWN[key]();
      renderLegend();
      updateLegend(bars.length - 1);   // WT 的读数只在显示时进顶部图例
      onShownChange();
    }

    // ---------- 模拟交易叠加 ----------
    // 页面每次拿到新的面板数据都会交进来(轮询是每秒一次); 内容没变就不动图表。

    function renderPaperOverlays() {
      if (!paperState) return;
      paperMarkerList = PaperPanel.buildMarkers(paperState.contractTrades, bars);
      applyCandleMarkers();
      const lines = PaperPanel.priceLines(paperState);
      const lineKey = JSON.stringify(lines);
      if (lineKey === paperLineKey) return;
      paperLines.forEach((line) => candleSeries.removePriceLine(line));
      paperLines = lines.map((line) => candleSeries.createPriceLine({
        price: line.price, color: line.color, title: line.title, lineWidth: 1, axisLabelVisible: true,
        lineStyle: line.style === "dashed" ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      }));
      paperLineKey = lineKey;
    }

    // ---------- 足迹图视图切换与数据 ----------

    function ohlcOf(t) {   // bars 按 time 升序, 二分查找出 footprint bar 对应的 K线 OHLC
      let lo = 0, hi = bars.length - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (bars[mid].time === t) return bars[mid];
        if (bars[mid].time < t) lo = mid + 1; else hi = mid - 1;
      }
      return null;
    }

    const toFpItem = (b) => {
      const item = { time: b.time, levels: b.levels, coverage: b.coverage };
      const k = ohlcOf(b.time);
      if (k) { item.open = k.open; item.high = k.high; item.low = k.low; item.close = k.close; }
      return item;
    };

    // 按 settings.view 切换 K线 / 足迹图
    function setView() {
      const isFp = settings.view === "footprint";
      candleSeries.applyOptions({ visible: !isFp });
      applyEmaVisibility();
      fpSeries.applyOptions({ visible: isFp });
      applyWtVisibility();     // 足迹图下 WaveTrend 也隐藏
      applyBandVisibility();   // 足迹图下强制隐藏叠加带, 切回 K 线按开关恢复
      renderLegend();          // 足迹图下主图指标全部隐藏, 图例跟着收起
      if (isFp) {
        fpBarSpacing = chart.timeScale().options().barSpacing;
        chart.timeScale().applyOptions({ barSpacing: 60 });
      } else if (fpBarSpacing != null) {
        chart.timeScale().applyOptions({ barSpacing: fpBarSpacing });
        fpBarSpacing = null;
      }
      if (cfg) connectWs();  // 订阅视图需求并获得完整快照，补齐未观看期间的足迹。
    }

    function applyFootprint(data, replace = false) {
      if (!data || data.revision < fpRevision) return;
      fpRevision = data.revision;
      const oldLast = fpBars[fpBars.length - 1]?.time;
      const canUpdate = !replace && data.bars.length === 1 && fpBars.length < 800 &&
                        (oldLast == null || data.bars[0].time >= oldLast);
      fpBars = FlowData.mergeBars(replace ? [] : fpBars, data.bars);
      const first = bars[0]?.time;
      if (first != null) fpBars = fpBars.filter((b) => b.time >= first);
      fpSeries.applyOptions({ tickSize: data.tickSize });
      if (settings.view === "footprint") {
        if (canUpdate) fpSeries.update(toFpItem(data.bars[0]));
        else fpSeries.setData(fpBars.map(toFpItem));
      }
      updateLegend(bars.length - 1);
    }

    // ---------- 拆分粒度 ----------

    // 本周期合法的拆分粒度: cfg 是本图上次加载时服务端下发的, 拿到之前按本地表
    function ltfOptions() {
      const options = cfg && cfg.tf === tf ? cfg.ltfOptions : null;
      const legal = options && options.length ? options.filter((s) => s > 0) : localLtfOptions(tf);
      return legal.length ? legal : [1, 5, 10];
    }

    // 本图该请求的粒度: tick 口径恒为 0; K 线口径取工具栏的选择, 本周期用不了(15/30 不能整除 10)时
    // 回落到本周期下最粗的合法粒度(即主周期本身)。工具栏上的选择不动, 其他周期的图可能正用着。
    function currentLtf() {
      const wanted = FlowData.splitLtf(settings.cvdSource, settings.klineLtf);
      const legal = ltfOptions();
      return wanted === 0 || legal.includes(wanted) ? wanted : legal[legal.length - 1];
    }

    // ---------- 数据加载与实时推送 ----------

    function clearLoadRetry() {
      clearTimeout(retryTimer);
      retryTimer = null;
    }

    function retryAfterError(message, generation) {
      if (generation !== loadGeneration) return;
      clearLoadRetry();
      if (retryAttempts >= RETRY_MAX_ATTEMPTS) {
        onStatus(false, `${message}（已重试 ${retryAttempts} 次，请检查合约代码后手动刷新）`);
        return;
      }
      retryAttempts += 1;
      onStatus(false, `${message}（第 ${retryAttempts} 次重试中…）`);
      retryTimer = setTimeout(() => {
        if (generation === loadGeneration) loadHistory(generation);
      }, RETRY_DELAY_MS);
    }

    async function loadHistory(generation) {
      if (generation == null) {
        generation = ++loadGeneration;  // 用户切换开始新一轮加载；自动重试沿用原 generation。
        retryAttempts = 0;
      }
      if (generation !== loadGeneration) return;
      clearLoadRetry();
      if (loadController) loadController.abort();
      const controller = new AbortController();
      loadController = controller;
      onStatus(false, "加载中…");
      ++wsGeneration;
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
      clearTimeout(watchdog);
      if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
      }
      ltf = currentLtf();
      try {
        const resp = await fetch("/api/history?symbol=" + encodeURIComponent(symbol) +
                                 "&ltf=" + ltf + "&tf=" + tf, { signal: controller.signal });
        if (generation !== loadGeneration) return;
        if (!resp.ok) {
          let detail = `HTTP ${resp.status}`;
          try {
            const body = await resp.json();
            if (body && body.detail) detail = body.detail;
          } catch (ignored) { /* 非 JSON 错误响应, 保留 HTTP 码 */ }
          throw new Error(detail);
        }
        const data = await resp.json();
        if (generation !== loadGeneration) return;
        if (data.pending) {
          const ingestStatus = data.status;
          if (ingestStatus && ingestStatus.status === "error") {
            onStatus(false, "行情错误: " + (ingestStatus.lastError || "未知错误"));
          } else {
            onStatus(false, "等待行情…");
          }
          retryTimer = setTimeout(() => {
            if (generation === loadGeneration) loadHistory(generation);
          }, 3000);
          return;
        }
        cfg = data.cfg;
        bars = data.bars;
        barRevision = -1;
        fpRevision = -1;
        onConfig();
        renderAll();
        chart.timeScale().scrollToRealTime();
        connectWs();
        retryAttempts = 0;
      } catch (error) {
        // 旧请求即使在响应体解析阶段失败，也不能修改新视图状态或关闭它的 WebSocket。
        if (generation === loadGeneration && !controller.signal.aborted) {
          retryAfterError(error.message, generation);
        }
      } finally {
        if (loadController === controller) loadController = null;
      }
    }

    function onBars(updates) {
      if (!updates.length) return;
      const oldLast = bars[bars.length - 1]?.time;
      const onlyLast = updates.length === 1 && updates[0].time === oldLast;
      bars = FlowData.mergeBars(bars, updates);
      if (onlyLast) updateLast();
      else renderAll();  // 补齐/修订历史和窗口裁剪时，所有图表使用同一份数据。
      if (settings.view === "footprint" && fpBars.length) {
        const latest = fpBars[fpBars.length - 1];
        if (onlyLast && latest.time === oldLast) fpSeries.update(toFpItem(latest));
        else fpSeries.setData(fpBars.map(toFpItem));
      }
    }

    function connectWs() {
      const generation = ++wsGeneration;
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
      clearTimeout(watchdog);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      onStatus(false, "同步中…");
      const wsScheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${wsScheme}://${location.host}/ws?symbol=${encodeURIComponent(symbol)}&ltf=${ltf}&tf=${tf}&footprint=${settings.view === "footprint"}`);
      ws = socket;
      let synced = false;
      const armWatchdog = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => {
          if (generation === wsGeneration) socket.close();
        }, synced ? 45000 : 100000);
      };
      socket.onopen = () => {
        if (generation === wsGeneration) armWatchdog();
      };
      socket.onclose = () => {
        if (generation !== wsGeneration) return;
        clearTimeout(watchdog);
        onStatus(false, "已断开, 重连补齐中…");
        reconnectTimer = setTimeout(() => {
          if (generation === wsGeneration) connectWs();
        }, 3000);
      };
      socket.onerror = () => {
        if (generation === wsGeneration) onStatus(false, "连接错误");
      };
      socket.onmessage = (ev) => {
        if (generation !== wsGeneration) return;
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "ping") {
            if (msg.error || msg.status?.status !== "connected") {
              onStatus(false, msg.error || "行情源: " + (msg.status?.lastError || msg.status?.status || "未知"));
            } else if (synced) onStatus(true, "已连接");
          } else if (msg.symbol === symbol && msg.type === "snapshot" && msg.ltf === ltf && msg.tf === tf) {
            cfg = msg.cfg;
            bars = FlowData.mergeBars([], msg.bars);
            barRevision = msg.revision;
            fpRevision = -1;
            onConfig();
            renderAll();
            if (msg.footprint) applyFootprint(msg.footprint, true);
            else { fpBars = []; fpSeries.setData([]); }
            synced = true;
            onStatus(true, "已连接");
          } else if (synced && msg.symbol === symbol && msg.tf === tf) {
            if (msg.type === "bars" && msg.ltf === ltf && msg.revision > barRevision) {
              onBars(msg.bars);
              barRevision = msg.revision;
            } else if ((msg.type === "footprints" || msg.type === "footprint_snapshot") && msg.revision > fpRevision) {
              applyFootprint(msg, msg.type === "footprint_snapshot");
            }
          }
          armWatchdog();
        } catch (error) {
          onStatus(false, "数据同步失败: " + error.message);
          socket.close();
        }
      };
    }

    // 切换本图周期: 先清掉旧周期的画面(新数据到之前不能还显示旧周期的 K 线), 再按新周期重新加载;
    // 加载流程会作废进行中的请求、重试与旧周期的推送连接。
    function setTf(next) {
      if (next === tf || !TF_CHOICES.includes(next)) {
        badge.value = String(tf);
        return;
      }
      tf = next;
      badge.value = String(tf);
      setMeasurePhase("off");   // 量的是旧周期的 bar, 换周期就清掉
      bars = [];
      fpBars = [];
      barRevision = -1;
      fpRevision = -1;
      chart.timeScale().applyOptions({ secondsVisible: tf < 60 });
      fpSeries.setData([]);
      if (cfg) renderAll();
      loadHistory();
      onTfChange();
    }

    // 本图对应时刻 t 的 bar: 起点不晚于 t 的最后一根。bar 从周期的整数倍开始, 所以 10s 的 t 落到包含它的
    // 30s bar, 30s 的 t 落到同一时刻的第一根 10s bar; 本图缺这一根就是前一根
    function barAt(t) {
      const i = indexAtOrBefore(bars, t);
      return i < 0 ? null : bars[i];
    }

    // ---------- 对页面的接口 ----------
    // 工具栏改了 settings 之后, 页面按改动调对应的入口
    return {
      get tf() { return tf; },
      setTf,
      el,
      shown: () => ({ ...shown }),
      load: () => loadHistory(),       // 重新加载(CVD口径/拆分粒度变了), 作废进行中的请求与重试
      setView,                         // 视图(K线/足迹图)变了
      renderSuite,                     // 模式/阈值变了
      renderWtMarks() {                // WT信号档位变了: 只换箭头, 振荡线不用重画
        if (derived && derived.wt) renderWtMarks();
      },
      rebuildBand() {                  // 带宽变了: 中线/状态都不受影响, 不必整体 derive(), 重算带即可
        if (derived && derived.lw) {
          derived.band = deriveBand();
          renderBand();
        }
        renderLegend();                // 图例里的带宽跟着变
      },
      refreshLegend: () => updateLegend(bars.length - 1),
      setPaperState(state) {
        paperState = state;
        renderPaperOverlays();
      },
      ltfOptions,
      // 联动: 可视范围按时间读写(两张图 bar 数不同, 只能按时间对齐), 十字光标按时间摆
      visibleTimeRange() {
        const range = chart.timeScale().getVisibleLogicalRange();
        if (!range || !bars.length) return null;
        return { from: timeAtLogical(bars, tf, range.from), to: timeAtLogical(bars, tf, range.to) };
      },
      setVisibleTimeRange(range) {
        if (!bars.length) return;
        const from = logicalAtTime(bars, tf, range.from), to = logicalAtTime(bars, tf, range.to);
        const current = chart.timeScale().getVisibleLogicalRange();
        // 已经对齐就不再设: 设了会再触发一次范围变化, 两张图来回同步停不下来
        if (current && Math.abs(current.from - from) < 0.01 && Math.abs(current.to - to) < 0.01) return;
        chart.timeScale().setVisibleLogicalRange({ from, to });
      },
      // 只对齐右边缘(两张图周期差太多, 完整同步会把大周期压成一两根时用): 本图可视的 bar 数不变,
      // 右边缘移到 time。time 越过了本图最后一根(对方在看最新; 大周期图的留白折成小周期能有上百根)时,
      // 本图停在最新、只留自己的右侧留白, 不跟着推进一大片空白
      alignRightEdge(time) {
        const current = chart.timeScale().getVisibleLogicalRange();
        if (!current || !bars.length) return;
        const last = bars.length - 1;
        let to = logicalAtTime(bars, tf, time);
        if (to > last) to = current.to > last ? current.to : last + RIGHT_OFFSET;
        if (Math.abs(current.to - to) < 0.01) return;
        chart.timeScale().setVisibleLogicalRange({ from: to - (current.to - current.from), to });
      },
      showCrosshair(time, price) {     // price 为 null 时横线落在那根 bar 的收盘价上
        const bar = barAt(time);
        if (!bar) chart.clearCrosshairPosition();
        else chart.setCrosshairPosition(price ?? bar.close, bar.time, candleSeries);
      },
      hideCrosshair: () => chart.clearCrosshairPosition(),
      cancelMeasure() {                // Esc: 取消测量, 已量完留在图上的也清掉
        if (measurePhase !== "off") setMeasurePhase("off");
      },
      // 当前测量: 阶段、两端与读数(两端还没定时 stats 为 null)
      measurement() {
        return { phase: measurePhase, a: measureA, b: measureB,
                 stats: measureA && measureB && bars.length ? measureStats(bars, tf, measureA, measureB) : null };
      },
      // 以下只供测试直接读写图表内部状态
      get bars() { return bars; },
      set bars(value) { bars = value; },
      get cfg() { return cfg; },
      set cfg(value) { cfg = value; },
      get derived() { return derived; },
      derive, levelOf, renderAll, wtDivergence,
    };
  }

  // 渲染类与时间换算一并导出, 测试直接检查
  root.ChartView = { create, FootprintRenderer, BandRenderer, BandSeries, TF_CHOICES, tfLabel, localLtfOptions,
                     indexAtOrBefore, timeAtLogical, logicalAtTime, formatDuration, measureStats, measureLines };
})(typeof globalThis !== "undefined" ? globalThis : this);
