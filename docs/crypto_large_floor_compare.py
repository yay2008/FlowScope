"""Compare 100%, 80% and 60% notional floors with identical historical P99 baselines.

Uses the audited event archive produced by crypto_large_trade_replay.py.
The floor does not affect event merging, baseline membership or its quantile.
Raw-print replays verify that applying new floors to saved events is exact.
Only research output is written; live settings are never changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docs.crypto_large_trade_replay import (AuditDetector, BJ, DAY_MS, DTYPE,  # noqa: E402
    load_frame, log, markouts)
from large_trades import LargeTradeDetector, settings_for  # noqa: E402

SCALES = (1.0, 0.8, 0.6)
TIMEFRAMES = (10, 30, 60, 300)
LABELS = {0: "原有大单", 1: "80%新增", 2: "60%进一步新增", 3: "60%仍被金额线拦截"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quantiles(values):
    values = np.asarray(values)
    values = values[np.isfinite(values)]
    return {str(p): float(np.quantile(values, p)) for p in (.1, .5, .9, 1)} if len(values) else {}


def raw_validation(source, meta, scales=SCALES):
    """Replay an identical source slice through the unmodified detector at all floors."""
    raw = load_frame(source["file"], meta["symbol"], meta["multiplier"]).iloc[:100_000]
    baseline = AuditDetector(meta["symbol"], meta["settings"])
    for row in raw.itertuples(index=False, name=None):
        baseline.trade(*row)
    baseline.finish()
    events = np.array(baseline.records, dtype=DTYPE)
    ready = events["category"] != 0
    p99 = events["qty"] > events["p99_qty"]
    outcomes = []
    for scale in scales:
        settings = dict(meta["settings"], minNotional=meta["settings"]["minNotional"] * scale)
        live = LargeTradeDetector(meta["symbol"], "aggregate" if meta["symbol"].startswith("BINANCE.") else "trade", settings)
        for row in raw.itertuples(index=False, name=None):
            live.trade(*row)
        passed = events[ready & p99 & (events["notional"] >= settings["minNotional"])]
        assert set(passed["id"]) == set(live.events), (meta["symbol"], scale)
        for event in passed:
            original = live.events[int(event["id"])]
            assert event["qty"] == original["qty"]
            assert event["price"] == original["price"]
            assert event["p99_qty"] == original["threshold"]
        assert baseline._sorted == live._sorted
        assert list(baseline._lagged) == list(live._lagged)
        outcomes.append(dict(scale=scale, accepted_events=len(passed), exact_match=True))
    return dict(symbol=meta["symbol"], raw_records=len(raw), scenarios=outcomes)


def load_candidates(input_dir, manifest):
    frames, closes, metas, validations, provenance = {}, {}, {}, [], []
    start, end = manifest["start_ms"], manifest["end_ms"]
    for path in sorted(input_dir.glob("*.events.npz")):
        symbol = path.name.removesuffix(".events.npz")
        meta = json.loads((input_dir / f"{symbol}.meta.json").read_text(encoding="utf-8"))
        current = settings_for(symbol, "aggregate" if symbol.startswith("BINANCE.") else "trade")
        if current != meta["settings"]:
            raise ValueError(f"Current settings changed for {symbol}; refresh the baseline replay first")
        events = np.load(path)["events"]
        future = markouts(events, 60)
        target = (events["ms"] >= start) & (events["ms"] < end)
        ready = events["category"] != 0
        relative = ready & (events["qty"] > events["p99_qty"])
        assert np.array_equal(relative, np.isin(events["category"], [2, 3]))
        selected = target & relative
        frame = pd.DataFrame.from_records(events[selected])
        frame["symbol"] = symbol
        frame["floor_usdt"] = meta["settings"]["minNotional"]
        frame["floor_fraction"] = frame.notional / frame.floor_usdt
        frame["qty_over_p99"] = frame.qty / frame.p99_qty
        frame["signed_return_60s_bps"] = future[selected]
        frame["band"] = np.select([frame.notional >= frame.floor_usdt,
                                    frame.notional >= frame.floor_usdt * .8,
                                    frame.notional >= frame.floor_usdt * .6], [0, 1, 2], default=3)
        frame["time_beijing"] = pd.to_datetime(frame.ms, unit="ms", utc=True).dt.tz_convert(BJ).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
        frame["utc_day"] = pd.to_datetime(frame.ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
        assert np.array_equal(frame.band.to_numpy() == 0, frame.category.to_numpy() == 2)
        frames[symbol], metas[symbol] = frame, meta
        ends = np.arange(start + 30_000, end + 1, 30_000)
        close_ids = np.searchsorted(events["ms"], ends - 1, side="right") - 1
        assert (close_ids >= 0).all()
        closes[symbol] = events["last_price"][close_ids]
        # A complete archive, not its small warm-up tail, provides enough validation events.
        archives = sorted([a for a in manifest["archive_files"] if a["symbol"] == symbol], key=lambda a: a["day"])
        source = next(a for a in archives if a["day"] == "2026-10-14")
        if digest(source["file"]) != source["sha256"]:
            raise ValueError("Validation archive changed")
        validation = raw_validation(source, meta)
        validations.append(validation)
        provenance.append(dict(symbol=symbol, events_file=str(path.resolve()), sha256=digest(path), settings=meta["settings"]))
        log(f"validated 100k raw records x 3 floors: {symbol}")
    return frames, closes, metas, validations, provenance


def event_summary(name, frame):
    out = []
    base = frame.band == 0
    for scale, last_band in zip(SCALES, (0, 1, 2)):
        selected = frame.band <= last_band
        added = selected & ~base
        rows = frame[added]
        out.append(dict(view=name, scale=scale, events=int(selected.sum()), added_events=int(added.sum()),
            added_event_ratio=float(added.sum() / base.sum()),
            notional_usdt=float(frame.notional[selected].sum()), added_notional_usdt=float(rows.notional.sum()),
            added_notional_ratio=float(rows.notional.sum() / frame.notional[base].sum()),
            added_amount_quantiles=quantiles(rows.notional), added_p99_ratio_quantiles=quantiles(rows.qty_over_p99),
            added_amount_over_floor_quantiles=quantiles(rows.floor_fraction),
            recovered_fixed_blocked_rate=float(added.sum() / (~base).sum()),
            added_buy_events=int((rows.side == 1).sum()), added_sell_events=int((rows.side == -1).sum())))
    return out


def make_bars(frame, start, end, tf):
    n = (end - start) // (tf * 1000)
    keys = ((frame.ms.to_numpy() - start) // (tf * 1000)) * 2 + (frame.side.to_numpy() == 1)
    bars = []
    for band in (0, 1, 2):
        selected = frame.band.to_numpy() <= band
        count = np.bincount(keys[selected], minlength=n*2).reshape(n, 2)
        qty = np.bincount(keys[selected], weights=frame.qty.to_numpy()[selected], minlength=n*2).reshape(n, 2).round(8)
        notional = np.bincount(keys[selected], weights=frame.notional.to_numpy()[selected], minlength=n*2).reshape(n, 2)
        bars.append(dict(count=count, qty=qty, notional=notional))
    return bars, keys


def marker_summary(name, frame, start, end, tf):
    bars, keys = make_bars(frame, start, end, tf)
    base = bars[0]["count"].ravel() > 0
    out = []
    for i, scale in enumerate(SCALES):
        present = bars[i]["count"].ravel() > 0
        added = (frame.band.to_numpy() > 0) & (frame.band.to_numpy() <= i)
        existing = base[keys[added]]
        money = frame.notional.to_numpy()[added]
        affected = base & (bars[i]["count"].ravel() > bars[0]["count"].ravel())
        out.append(dict(view=name, tf_seconds=tf, scale=scale, markers=int(present.sum()),
            added_markers=int((present & ~base).sum()), added_marker_ratio=float((present & ~base).sum() / base.sum()),
            markers_with_added_volume=int(affected.sum()),
            original_markers_unchanged_volume=int((base & ~affected).sum()),
            added_events_on_existing_marker_rate=float(existing.mean()) if len(existing) else None,
            added_amount_on_existing_marker_rate=float(money[existing].sum()/money.sum()) if money.sum() else None,
            markers_per_100_bars=float(present.sum()/len(bars[0]["count"])*100)))
    return out, bars


def size_unit(qty, floor_qty):
    positive = np.sort(qty[qty > 0])
    return max(floor_qty, positive[int((len(positive)-1)*.9)]/16) if len(positive) >= 20 else floor_qty


def radius(qty, unit):
    return np.clip(4 * np.sqrt(np.abs(qty) / unit), 3, 20)


def validate_renderer(checks, output):
    """Cross-check offline arithmetic against the unchanged production JavaScript."""
    path = output / "renderer_checks.json"
    path.write_text(json.dumps(checks), encoding="utf-8")
    javascript = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const renderer = require(process.argv[2]);
const checks = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const near = (a, b) => Math.abs(a-b) <= 1e-10 * Math.max(1, Math.abs(b));
let radiusCases = 0;
for (const c of checks) {
 const bars = c.qty.map(([sell,buy]) => ({large:{count:Number(sell>0)+Number(buy>0),sell,buy}}));
 const configs = [{source:'aggregate',minNotional:c.min_floor,minQty:0,multiplier:1}];
 assert(near(renderer.sizeUnit(bars, configs, c.reference), c.unit), c.view+' size unit');
 for(let i=0;i<c.qty.length;i++) for(let side=0;side<2;side++) {
  assert(near(renderer.radius(c.qty[i][side],c.unit),c.radii[i][side]), c.view+' radius');
  radiusCases++;
 }
}
console.log(JSON.stringify({size_unit_cases:checks.length,radius_cases:radiusCases,passed:true,relative_tolerance:1e-10}));
"""
    completed = subprocess.run(["node", "-", str(ROOT / "static/large-trades.js"), str(path.resolve())],
        input=javascript, text=True, encoding="utf-8", capture_output=True, check=True)
    return json.loads(completed.stdout)


def size_and_previews(name, bars, close, min_floor, start):
    # Match the app's 800-bar snapshot. Every half hour is a separate display state.
    n = len(close)
    ratios = {i: {"unchanged": [], "volume_only": [], "actual": [], "floor_active": []} for i in (1, 2)}
    checks = []
    for stop in range(840, n + 1, 60):
        window = slice(stop - 800, stop)
        q0 = bars[0]["qty"][window]
        present = bars[0]["count"][window] > 0
        units = [size_unit(bars[i]["qty"][window], min_floor*SCALES[i]/close[stop-1]) for i in range(3)]
        r0 = radius(q0, units[0])
        for i in (1, 2):
            q = bars[i]["qty"][window]
            unchanged = present & (bars[i]["count"][window] == bars[0]["count"][window])
            actual = radius(q, units[i])
            ratios[i]["unchanged"].extend((actual[unchanged] / r0[unchanged] - 1).tolist())
            ratios[i]["actual"].extend((actual[present] / r0[present] - 1).tolist())
            ratios[i]["volume_only"].extend((radius(q, units[0])[present] / r0[present] - 1).tolist())
            ratios[i]["floor_active"].append(units[i] == min_floor*SCALES[i]/close[stop-1])
    stats = [dict(view=name, scale=SCALES[i], sample_windows=len(ratios[i]["floor_active"]),
        unchanged_volume_radius_change=quantiles(ratios[i]["unchanged"]),
        all_original_radius_change=quantiles(ratios[i]["actual"]),
        volume_only_radius_change=quantiles(ratios[i]["volume_only"]),
        floor_determines_unit_rate=float(np.mean(ratios[i]["floor_active"]))) for i in (1, 2)]
    # Two declared selection rules on the last, post-calibration UTC day.
    candidates = []
    for stop in range(n - 2880 + 60, n + 1, 60):
        sl = slice(stop-60, stop)
        gain = int(((bars[2]["count"][sl] > 0) & (bars[0]["count"][sl] == 0)).sum())
        candidates.append((gain, stop))
    ordered = sorted(candidates)
    examples = []
    for label, (gain, stop) in [("新增圆点数居中的窗口", ordered[len(ordered)//2]), ("新增圆点最多的窗口", ordered[-1])]:
        reference = float(close[stop-1])
        units = [size_unit(bars[i]["qty"][stop-800:stop], min_floor*SCALES[i]/reference) for i in range(3)]
        series = []
        for i in range(3):
            sl = slice(stop-60, stop)
            series.append(dict(scale=SCALES[i], unit=units[i], qty=bars[i]["qty"][sl].tolist(),
                               count=bars[i]["count"][sl].tolist(), notional=bars[i]["notional"][sl].tolist()))
            window = bars[i]["qty"][stop-800:stop]
            checks.append(dict(view=name, scale=SCALES[i], min_floor=min_floor*SCALES[i], reference=reference,
                               qty=window.tolist(), unit=units[i], radii=radius(window, units[i]).tolist()))
        examples.append(dict(label=label, added_60_markers=gain,
            start_ms=start+(stop-60)*30_000, end_ms=start+stop*30_000, series=series,
            time_beijing=datetime.fromtimestamp((start+(stop-60)*30_000)/1000, BJ).strftime("%m-%d %H:%M")))
    return stats, examples, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/research/crypto-large-replay-2026-10-17")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.input / "floor-comparison"
    output.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    manifest = json.loads((args.input / "manifest.json").read_text(encoding="utf-8"))
    for filename, key in [("large_trades.py", "detector_sha256"), ("large_trade_defaults.py", "defaults_sha256")]:
        if digest(ROOT / filename) != manifest[key]:
            raise ValueError("Detection code/defaults changed; rebuild the source audit")
    frames, closes, metas, validations, provenance = load_candidates(args.input, manifest)
    start, end = manifest["start_ms"], manifest["end_ms"]
    views = dict(frames)
    for coin in ("BTC", "ENA", "ZEC"):
        views[f"AGG.{coin}"] = pd.concat([frames[f"BINANCE.{coin}USDT.P"], frames[f"OKX.{coin}-USDT-SWAP"]], ignore_index=True)
        closes[f"AGG.{coin}"] = closes[f"BINANCE.{coin}USDT.P"]
    all_frames = pd.concat(frames.values(), ignore_index=True)
    summaries = event_summary("ALL_VENUES", all_frames)
    all_events, all_markers, all_sizes, previews, checks, examples, group_rows = [], [], [], {}, [], [], []
    for name, frame in views.items():
        all_events.extend(event_summary(name, frame))
        for tf in TIMEFRAMES:
            rows, bars = marker_summary(name, frame, start, end, tf)
            all_markers.extend(rows)
            if tf == 30:
                stats, preview, test_cases = size_and_previews(name, bars, closes[name], float(frame.floor_usdt.min()), start)
                all_sizes.extend(stats)
                previews[name] = preview
                checks.extend(test_cases)
        for band in (0, 1, 2, 3):
            group = frame[frame.band == band]
            valid = group.signed_return_60s_bps.dropna()
            group_rows.append(dict(view=name, band=band, label=LABELS[band], events=len(group),
                notional_usdt=float(group.notional.sum()), amount_median=float(group.notional.median()) if len(group) else None,
                p99_ratio_median=float(group.qty_over_p99.median()) if len(group) else None,
                mean_60s_bps=float(valid.mean()) if len(valid) else None,
                median_60s_bps=float(valid.median()) if len(valid) else None, valid_60s=len(valid)))
            if band in (1, 2) and not name.startswith("AGG."):
                for kind, column in [("金额最大", "notional"), ("相对P99最突出", "qty_over_p99")]:
                    top = group.nlargest(5, column).copy()
                    top["selection"] = kind
                    examples.extend(json.loads(top.to_json(orient="records")))
        log(f"compared event and marker changes: {name}")
    pd.DataFrame(all_events).drop(columns=[c for c in all_events[0] if c.endswith("quantiles")]).to_csv(output / "event_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(all_markers).to_csv(output / "marker_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(group_rows).to_csv(output / "incremental_bands.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(examples).to_csv(output / "event_examples.csv", index=False, encoding="utf-8-sig")
    all_frames[all_frames.band.isin([1, 2])].to_csv(output / "all_added_events.csv.gz", index=False, compression="gzip", float_format="%.12g")
    daily_rows = []
    for day, group in all_frames.groupby("utc_day"):
        daily_rows.extend([dict(row, utc_day=day) for row in event_summary("ALL_VENUES", group)])
    pd.DataFrame(daily_rows).drop(columns=[c for c in daily_rows[0] if c.endswith("quantiles")]).to_csv(output / "daily.csv", index=False, encoding="utf-8-sig")
    marginal = []
    for band in (1, 2):
        group = all_frames[all_frames.band == band]
        marginal.append(dict(band=band, events=len(group), notional_usdt=float(group.notional.sum()),
            p99_ratio_median=float(group.qty_over_p99.median()),
            p99_ratio_p90=float(group.qty_over_p99.quantile(.9)),
            above_twice_p99=int((group.qty_over_p99 >= 2).sum())))
    renderer_validation = validate_renderer(checks, output)
    result = dict(period_beijing=manifest["period_beijing"], start_ms=start, end_ms=end,
        generated_at=datetime.now(BJ).isoformat(), settings={s: m["settings"] for s, m in metas.items()},
        totals=summaries, event_summary=all_events, marker_summary=all_markers, size_summary=all_sizes,
        bands=group_rows, examples=examples, previews=previews, daily=daily_rows,
        marginal=marginal, validation=validations, renderer_validation=renderer_validation,
        provenance=provenance, source_manifest=str((args.input / "manifest.json").resolve()),
        detector_sha256=digest(ROOT / "large_trades.py"), renderer_sha256=digest(ROOT / "static/large-trades.js"))
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    write_report(result, output)
    log(f"REPORT {output / 'report.html'}")


def write_report(result, output):
    def table(rows):
        return pd.DataFrame(rows).to_html(index=False, border=0, escape=True)

    def percent(value):
        return f"{value:.1%}"

    def count(value):
        return f"{value:,.0f}"

    physical = set(result["settings"])
    totals = result["totals"]
    marker = pd.DataFrame(result["marker_summary"])
    headline = []
    for row in totals:
        marks = marker[(marker.view.isin(physical)) & (marker.tf_seconds == 30) & (marker.scale == row["scale"])]
        headline.append({"金额线": percent(row["scale"]), "事件总数": count(row["events"]),
            "比原来新增事件": count(row["added_events"]), "事件增加": percent(row["added_event_ratio"]),
            "新增成交额（亿USDT）": f"{row['added_notional_usdt']/1e8:.2f}",
            "成交额增加": percent(row["added_notional_ratio"]),
            "30秒圆点总数": count(marks.markers.sum()), "新增圆点": count(marks.added_markers.sum())})
    body = ["<h1>固定金额线降至 80% / 60%：大单与圆点对比</h1>",
        f"<p class='sub'>{html.escape(result['period_beijing'])} · P99 保持不变 · 8 个单所合约 + 3 个多所汇总视图</p>",
        "<p><a href='#circle-preview'>直接看三档圆点</a> · <a href='#contracts'>各合约增量</a> · <a href='#sizing'>尺寸变化</a> · <a href='#examples'>具体事件</a></p>",
        "<p>使用上一轮已校验的完整成交事件和逐事件历史 P99，金额线仅影响是否入选，不影响基线更新。另在每个合约10万条原始记录的同一切片上，逐事件核对原版检测器三档回放与复用P99再筛选的结果，编号、数量、价格、P99均匹配，三档最终基线相同。未修改线上参数。</p>",
        "<h2>三档总体差异</h2>", table(headline),
        "<p>总体仅加总8个单所合约，多所汇总视图单独列出，避免重复统计。圆点按一根K线、一个主动方向计数，同一根买卖最多两个。所有增量相对100%原金额线，除非注明‘60%进一步新增’。</p>"]
    extra_events = totals[2]["events"] - totals[1]["events"]
    physical30 = marker[marker.view.isin(physical) & (marker.tf_seconds == 30)]
    extra_markers = physical30[physical30.scale == .6].markers.sum() - physical30[physical30.scale == .8].markers.sum()
    extra_money = totals[2]["notional_usdt"] - totals[1]["notional_usdt"]
    body.append(f"<p><b>从80%继续降至60%</b>，再增加 {extra_events:,} 个事件、{extra_markers:,} 个30秒圆点，额外计入 {extra_money/1e8:.2f} 亿USDT成交额。80%找回原来被金额线拦截事件的 {totals[1]['recovered_fixed_blocked_rate']:.1%}；60%找回 {totals[2]['recovered_fixed_blocked_rate']:.1%}。</p>")
    marginal = result["marginal"]
    body.append(f"<p>80%新增组的数量/P99中位数为 <b>{marginal[0]['p99_ratio_median']:.2f}倍</b>；60%进一步新增组为 <b>{marginal[1]['p99_ratio_median']:.2f}倍</b>。后一组整体更接近P99，但仍有 {marginal[1]['above_twice_p99']:,} 个事件超过当时P99的两倍，不能把全部增量等同于无意义的小单。这次回放衡量覆盖和显示变化，尚不足以证明哪档更有交易价值。</p>")
    body.append("<h2 id='contracts'>各合约新增了多少</h2>")
    summary_rows = []
    for name in physical:
        e = {r["scale"]: r for r in result["event_summary"] if r["view"] == name}
        m = {r["scale"]: r for r in result["marker_summary"] if r["view"] == name and r["tf_seconds"] == 30}
        floor = result["settings"][name]["minNotional"]
        summary_rows.append({"合约": name, "金额线100%→80%→60% USDT": f"{floor:,.0f} → {floor*.8:,.0f} → {floor*.6:,.0f}",
            "80%新增事件": e[.8]["added_events"], "60%新增事件": e[.6]["added_events"],
            "80%新增30秒圆点": m[.8]["added_markers"], "60%新增30秒圆点": m[.6]["added_markers"]})
    body.append(table(sorted(summary_rows, key=lambda r: r["合约"])))
    body.append("<h2>新增事件的区别</h2><p>80%新增指金额介于原门槛80%至100%的事件；60%进一步新增指60%至80%之间的事件。两组都严格超过当时 P99，互不重叠。</p>")
    group_rows = []
    for row in result["bands"]:
        if row["view"] not in physical or row["band"] not in (1, 2):
            continue
        group_rows.append({"合约": row["view"], "新增组": row["label"], "事件数": row["events"],
            "成交金额中位数 USDT": f"{row['amount_median']:,.0f}",
            "数量/P99中位数": f"{row['p99_ratio_median']:.2f}×",
            "60秒同向价格变化中位数 bp": f"{row['median_60s_bps']:.2f}" if row["median_60s_bps"] is not None else "—"})
    body.append(table(group_rows))
    body.append("<p>价格变化以事件最后一笔成交价为起点，主动买用后续涨幅、主动卖用后续跌幅；窗口不足或数据不连续时排除。它只描述后续行情，未扣费用、未控制市场状态，密集事件也不是独立样本，不能当作策略收益。</p>")
    body.append("<h2>圆点：新增位置与已有量增加</h2>")
    chart_rows = []
    for tf in TIMEFRAMES:
        for scale in (.8, .6):
            rows = marker[marker.view.isin(physical) & (marker.tf_seconds == tf) & (marker.scale == scale)]
            base = marker[marker.view.isin(physical) & (marker.tf_seconds == tf) & (marker.scale == 1)]
            chart_rows.append({"K线周期": f"{tf}秒", "金额线": percent(scale), "原有圆点": int(base.markers.sum()),
                "新增圆点": int(rows.added_markers.sum()), "圆点增加": percent(rows.added_markers.sum()/base.markers.sum()),
                "已有圆点中量增加的个数": int(rows.markers_with_added_volume.sum())})
    body.append(table(chart_rows))
    body.append("<h3>多所汇总图（两所分别判定，再按同一K线同一方向合计）</h3>")
    agg_rows = []
    for name in ("AGG.BTC", "AGG.ENA", "AGG.ZEC"):
        base = marker[(marker.view == name) & (marker.tf_seconds == 30) & (marker.scale == 1)].iloc[0]
        for scale in (.8, .6):
            row = marker[(marker.view == name) & (marker.tf_seconds == 30) & (marker.scale == scale)].iloc[0]
            agg_rows.append({"视图": name, "金额线": percent(scale), "原有30秒圆点": int(base.markers),
                "新增30秒圆点": int(row.added_markers), "增加": percent(row.added_marker_ratio),
                "新增事件落在原有圆点的比例": percent(row.added_events_on_existing_marker_rate)})
    body.append(table(agg_rows))
    body.append("<h2 id='circle-preview'>同一时段的圆点预览</h2><p>只画主动买卖圆点的时间分布，不画价格K线。三个面板使用同一时段、同一坐标。紫色外圈表示80%档已经新增的位置，橙色外圈表示60%档才新增的位置。默认按各档线上公式缩放；也可以固定原尺寸单位，单独观察成交量和位置变化。</p>")
    body.append("<p>模拟在该时点重新加载最近800根30秒K线，按当前前端公式计算各档尺寸单位。窗口取最后一个完整UTC日的48个半小时段：按60%新增圆点数排序，分别展示居中窗口和最多窗口，后者有意展示最明显的差异。</p>")
    body.append("<div class='controls'><label>视图 <select id='view'></select></label><label>时段 <select id='window'></select></label><label><input id='fixed-size' type='checkbox'>固定100%档的尺寸单位</label></div><div id='preview'></div>")
    body.append("<h2 id='sizing'>已有圆点也可能因尺寸单位变化而放大</h2><p>前端尺寸单位取‘金额线折成币数’和‘本图P90侧量÷16’中的较大值。降低金额线会改变这个单位。下表每半小时抽取一次最近800根30秒K线，统计三档间成交量完全没变的旧圆点，其半径变化中位数；这是重复观察的显示状态，不是独立成交样本。尺寸取决于已加载的数据及单位重算时机，本报告固定800根窗口并模拟重新加载；持续运行或加载更多历史时可能不同。</p>")
    size_rows = []
    for row in result["size_summary"]:
        size_rows.append({"视图": row["view"], "金额线": percent(row["scale"]),
            "量未变旧圆点：半径变化中位数": percent(row["unchanged_volume_radius_change"].get("0.5", 0)),
            "金额线主导尺寸单位的窗口比例": percent(row["floor_determines_unit_rate"]), "窗口数": row["sample_windows"]})
    body.append(table(size_rows))
    body.append("<h2 id='examples'>具体新增事件</h2>")
    sample_rows = []
    for symbol in ("BINANCE.BTCUSDT.P", "OKX.BTC-USDT-SWAP", "BINANCE.ZECUSDT.P", "OKX.ENA-USDT-SWAP"):
        for band in (1, 2):
            candidates = [r for r in result["examples"] if r["symbol"] == symbol and r["band"] == band and r["selection"] == "相对P99最突出"]
            if not candidates:
                continue
            row = max(candidates, key=lambda r: r["qty_over_p99"])
            sample_rows.append({"合约": symbol, "新增组": LABELS[band], "北京时间": row["time_beijing"],
                "方向": "买" if row["side"] > 0 else "卖", "金额 USDT": f"{row['notional']:,.2f}",
                "当时P99折算 USDT": f"{row['p99_notional']:,.2f}", "数量/P99": f"{row['qty_over_p99']:.2f}×"})
    body.append(table(sample_rows))
    body.append("<h2>分日检查</h2><p>下面按UTC日（北京时间当日08:00至次日08:00）切分。原金额线的标定包含10月14日归档，最后一天10月15日UTC不属于该标定日。</p>")
    daily = [{"UTC日": r["utc_day"], "金额线": percent(r["scale"]), "新增事件": r["added_events"],
              "事件增加": percent(r["added_event_ratio"]), "新增成交额（亿USDT）": f"{r['added_notional_usdt']/1e8:.2f}"}
             for r in result["daily"] if r["scale"] < 1]
    body.append(table(daily))
    rv = result["renderer_validation"]
    body.append(f"<p>计算校验：{len(result['validation'])} 个合约 × 100,000 条原始记录 × 3 档金额线，各档均与复用P99再筛选的结果匹配；{rv['size_unit_cases']} 组尺寸单位、{rv['radius_cases']:,} 次半径计算与当前前端 JavaScript 一致（相对误差容限1e-10）。报告内嵌同一份前端半径函数。</p>")
    body.append("<p><a href='event_summary.csv'>逐合约事件统计</a> · <a href='marker_summary.csv'>各周期圆点统计</a> · <a href='incremental_bands.csv'>两档互斥增量统计</a> · <a href='event_examples.csv'>典型事件与编号</a> · <a href='all_added_events.csv.gz'>全部新增事件</a> · <a href='daily.csv'>分日统计</a> · <a href='results.json'>完整结果与校验信息</a> · <a href='../report.html'>上一轮门槛审计</a></p>")
    payload = json.dumps(result["previews"], ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    renderer = (ROOT / "static/large-trades.js").read_text(encoding="utf-8")
    javascript = r"""
const PREVIEWS = __PAYLOAD__;
const view = document.getElementById('view'), windowSelect = document.getElementById('window');
const number = v => Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3});
const time = ms => new Date(ms).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
for (const name of Object.keys(PREVIEWS)) { const o=document.createElement('option'); o.value=name; o.textContent=name; view.append(o); }
view.value='AGG.BTC';
function windows(){ windowSelect.replaceChildren(); PREVIEWS[view.value].forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=p.label+' · '+p.time_beijing;windowSelect.append(o);}); draw(); }
function draw(){
 const p=PREVIEWS[view.value][Number(windowSelect.value)], fixed=document.getElementById('fixed-size').checked;
 const baseline=p.series[0], rows=[];
 for (const s of p.series){
  const unit=fixed?baseline.unit:s.unit; let circles='',marks=0,added=0;
  for(let i=0;i<60;i++) for(let side=0;side<2;side++){
   if(!s.count[i][side])continue;
   const fresh=!baseline.count[i][side], color=side?'#30cf9b':'#ff6978'; marks++; if(fresh)added++;
   const outline=fresh?(p.series[1].count[i][side]?'#7460d3':'#c27600'):'none';
   const x=70+(i+.5)*18,y=side?99:45,r=FlowLargeTrades.radius(s.qty[i][side],unit);
   const title=`${time(p.start_ms+i*30000)} ${side?'主动买':'主动卖'} · ${number(s.qty[i][side])}币 · ${number(s.notional[i][side])} USDT · ${s.count[i][side]}事件 · 半径${r.toFixed(2)}px`;
   circles+=`<g><title>${title}</title><circle cx="${x}" cy="${y}" r="${r}" fill="${color}" fill-opacity=".28" stroke="${outline}" stroke-width="1.4"/><text x="${x}" y="${y+3.5}" text-anchor="middle" font-size="10" font-weight="bold" fill="${color}">${side?'+':'−'}</text></g>`;
  }
  let axis='';for(let i=0;i<=60;i+=10){const x=70+i*18;axis+=`<line x1="${x}" y1="20" x2="${x}" y2="118" stroke="#dbe2eb"/><text x="${x}" y="139" text-anchor="middle" font-size="11" fill="#52647a">${time(p.start_ms+i*30000).slice(0,5)}</text>`;}
  rows.push(`<section class="plot"><b>${Math.round(s.scale*100)}% 金额线</b><span> ${marks} 个圆点 · 新位置 ${added} 个 · 尺寸单位 ${number(unit)}币</span><div class="svgwrap"><svg viewBox="0 0 1200 150" role="img" aria-label="${Math.round(s.scale*100)}%金额门槛圆点分布">${axis}<text x="10" y="49" font-size="12" fill="#bd4860">主动卖</text><text x="10" y="103" font-size="12" fill="#16855e">主动买</text>${circles}</svg></div></section>`);
 }
 document.getElementById('preview').innerHTML=rows.join('');
}
view.addEventListener('change',windows);windowSelect.addEventListener('change',draw);document.getElementById('fixed-size').addEventListener('change',draw);windows();
""".replace("__PAYLOAD__", payload)
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>金额线80%与60%回放对比</title><style>
body{font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif;max-width:1450px;margin:32px auto;padding:0 24px;background:#f5f7fb;color:#172b45}h1,h2,h3{color:#173f70}h2{margin-top:36px}.sub{color:#52647a}table{border-collapse:collapse;width:100%;font-size:13px;background:white;margin:16px 0;display:block;overflow-x:auto}th,td{padding:9px 12px;border-bottom:1px solid #dce3eb;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{background:#e6eef8}a{color:#075eb6}p{max-width:1180px}.controls{display:flex;flex-wrap:wrap;gap:18px;padding:16px;background:#e6eef8;border-radius:9px}select{padding:6px;max-width:400px}.plot{background:white;border:1px solid #dce3eb;border-radius:9px;margin:12px 0;padding:12px 16px}.plot span{color:#52647a;font-size:13px}.svgwrap{overflow-x:auto}svg{display:block;width:100%;min-width:800px}
</style><body>""" + "\n".join(body) + "<script>" + renderer + "</script><script>" + javascript + "</script></body></html>"
    (output / "report.html").write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()
