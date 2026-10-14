/* 合约每个连续交易时段的截止标记。时间与图表一致: 已加北京时间偏移的秒数。
 * 只为实际有 K 线的时段生成标记, 不用工作日猜节假日, 不往行情里插入虚构的 K 线。
 */
(function (root) {
  "use strict";
  const DAY = 86400;

  function seconds(value) {
    const match = /^(\d{1,2}):([0-5]\d):([0-5]\d)$/.exec(String(value));
    if (!match || Number(match[1]) >= 48) return null;
    return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
  }

  function periodsOf(value) {
    if (!Array.isArray(value)) return [];
    const periods = [];
    for (const pair of value) {
      if (!Array.isArray(pair) || pair.length !== 2) continue;
      const start = seconds(pair[0]);
      let end = seconds(pair[1]);
      if (start == null || end == null || start === end) continue;
      if (end < start) end += DAY;
      if (end > start && end - start <= DAY) periods.push({ start, end });
    }
    return periods.sort((a, b) => a.start - b.start);
  }

  function before(bars, time) {
    let lo = 0, hi = bars.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (bars[mid].time < time) lo = mid + 1;
      else hi = mid;
    }
    return lo - 1;
  }

  function tradingSeconds(periods, from, to) {
    return periods.reduce((sum, p) => sum + Math.max(0, Math.min(to, p.end) - Math.max(from, p.start)), 0);
  }

  function markers(bars, tf, tradingTime) {
    if (!bars.length || !(tf > 0) || !tradingTime) return [];
    const blocks = [periodsOf(tradingTime.day), periodsOf(tradingTime.night)];
    // 凌晨 K 线属于前一天起算的夜盘; 25:00/26:30 保留到算绝对时间时再换日。
    const anchors = new Set();
    for (const bar of bars) {
      const day = Math.floor(bar.time / DAY) * DAY;
      anchors.add(day - DAY);
      anchors.add(day);
    }
    const ends = new Map();
    for (const anchor of anchors) {
      for (const block of blocks) {
        const periods = block.map((p) => ({ start: anchor + p.start, end: anchor + p.end }));
        for (const period of periods) {
          const i = before(bars, period.end);
          if (i < 0) continue;
          const bar = bars[i];
          if (!periods.some((p) => p.start <= bar.time && bar.time < p.end)) continue;
          const elapsed = tradingSeconds(periods, bar.time, period.end);
          // 大周期可能包含几个小节, 用实际交易秒数判断是否覆盖; 缺掉整节行情则不凭空补标记。
          if (bar.time < period.start && elapsed > tf) continue;
          const next = bars[i + 1];
          const stop = Math.min(next?.time ?? Infinity, periods[periods.length - 1].end);
          const duration = Math.min(tf, tradingSeconds(periods, bar.time, stop));
          if (!(duration > 0)) continue;
          // K 线时间是起点, 截止在末根右边界。休市被时间轴压缩, 不能画到复市后的第一根上。
          // 尚未到来的本节截止按本周期投影到右侧留白; 大周期内的几个截止按交易时长分布。
          let offset = elapsed / duration - 0.5;
          if (next) offset = Math.min(offset, 0.5);
          const clock = new Date(period.end * 1000).toISOString().slice(11, 19);
          const label = `${clock.endsWith(":00") ? clock.slice(0, 5) : clock} 截止`;
          ends.set(period.end, { time: period.end, barTime: bar.time, offset, label });
        }
      }
    }
    return [...ends.values()].sort((a, b) => a.time - b.time);
  }

  class SessionEndPrimitive {
    constructor(timeScale, labels = true) {
      this._timeScale = timeScale;
      this._labels = labels;
      this._markers = [];
      this._requestUpdate = null;
      this._views = [{ zOrder: () => "top", renderer: () => ({ draw: (target) => this._draw(target) }) }];
    }
    attached({ requestUpdate }) { this._requestUpdate = requestUpdate; }
    detached() { this._requestUpdate = null; }
    updateAllViews() {}
    paneViews() { return this._views; }
    setMarkers(value) {
      this._markers = value;
      if (this._requestUpdate) this._requestUpdate();
    }
    _draw(target) {
      if (!this._markers.length) return;
      const scale = this._timeScale();
      const spacing = scale.options().barSpacing;
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        ctx.save();
        ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.lineWidth = 1;
        let labelRight = -Infinity;
        for (const marker of this._markers) {
          // 用实际时间坐标定位, 其它 series 暂时保留旧窗口时也不依赖 bar 下标。
          const base = scale.timeToCoordinate(marker.barTime);
          if (base == null) continue;
          const x = base + marker.offset * spacing;
          if (x < 0 || x > mediaSize.width) continue;
          ctx.strokeStyle = "rgba(224, 175, 88, 0.5)";
          ctx.setLineDash([3, 4]);
          ctx.beginPath();
          ctx.moveTo(x, 0);
          ctx.lineTo(x, mediaSize.height);
          ctx.stroke();
          if (!this._labels) continue;
          const width = ctx.measureText(marker.label).width + 12;
          const left = Math.max(2, Math.min(x - width / 2, mediaSize.width - width - 2));
          if (left < labelRight + 6 || width + 4 > mediaSize.width) continue;
          const top = mediaSize.height - 23;
          ctx.fillStyle = "#292720";
          ctx.fillRect(left, top, width, 19);
          ctx.fillStyle = "#e0af58";
          ctx.fillText(marker.label, left + 6, top + 9.5);
          labelRight = left + width;
        }
        ctx.restore();
      });
    }
  }

  const api = { markers, SessionEndPrimitive };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlowSessions = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
