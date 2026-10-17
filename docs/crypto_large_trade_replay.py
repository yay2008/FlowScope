"""Audit the two large-trade gates against public crypto trade archives.

The live detector's trade(), merge, baseline and judgement code are reused.
Only storage/plotting retention is bypassed, so every final event is counted.
No live configuration or market-data store is modified.

Example (dates are UTC, end is exclusive):
  .venv/Scripts/python docs/crypto_large_trade_replay.py --start 2026-10-13 --end 2026-10-16
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from large_trades import LargeTradeDetector, settings_for  # noqa: E402
from large_trade_defaults import MIN_NOTIONAL  # noqa: E402

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
LABELS = {0: "预热", 1: "两项均未通过", 2: "两项均通过", 3: "仅金额门槛拦截", 4: "仅P99拦截"}
DTYPE = np.dtype([
    ("id", "i8"), ("last_id", "i8"), ("ms", "i8"), ("side", "i1"),
    ("qty", "f8"), ("price", "f8"), ("last_price", "f8"), ("notional", "f8"),
    ("p99_qty", "f8"), ("p99_notional", "f8"), ("mean_qty", "f8"),
    ("baseline_n", "i4"), ("prints", "i4"), ("category", "i1"), ("reset", "?"),
])


def log(message):
    print(f"[{datetime.now(BJ):%H:%M:%S}] {message}", flush=True)


def request_bytes(url, attempts=3):
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlowScopeResearch"})
            with urllib.request.urlopen(req, timeout=55) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt == attempts - 1:
                raise
        except (OSError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(1 + attempt)


def archive_spec(symbol, day):
    venue, name = symbol.split(".", 1)
    if venue == "BINANCE":
        pair = name.removesuffix(".P")
        filename = f"{pair}-aggTrades-{day}.zip"
        url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{pair}/{filename}"
    else:
        filename = f"{name}-trades-{day}.zip"
        url = f"https://static.okx.com/cdn/okex/traderecords/trades/daily/{day.replace('-', '')}/{filename}"
    return filename, url


def dates_for(symbol, start_ms, end_ms):
    shift = 0 if symbol.startswith("BINANCE.") else 8 * 3_600_000
    day = (start_ms + shift) // DAY_MS * DAY_MS - shift
    while day < end_ms:
        yield datetime.fromtimestamp((day + shift) / 1000, UTC).strftime("%Y-%m-%d")
        day += DAY_MS


def download_one(cache, symbol, day):
    filename, url = archive_spec(symbol, day)
    path = cache / filename
    expected = None
    if symbol.startswith("BINANCE."):
        checksum_path = cache / (filename + ".CHECKSUM")
        if not checksum_path.exists():
            checksum_path.write_bytes(request_bytes(url + ".CHECKSUM"))
        expected = checksum_path.read_text().split()[0].lower()
    if not path.exists():
        body = request_bytes(url)
        if expected and hashlib.sha256(body).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch: {url}")
        partial = path.with_suffix(".zip.part")
        partial.write_bytes(body)
        partial.replace(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and digest != expected:
        raise ValueError(f"Cached checksum mismatch: {path}")
    with zipfile.ZipFile(path) as archive:
        # Reading each CSV later also verifies the ZIP CRC; reject malformed ZIPs now.
        if len(archive.namelist()) != 1:
            raise ValueError(f"Unexpected archive members: {path}")
    result = dict(symbol=symbol, day=day, url=url, file=str(path.resolve()),
                  bytes=path.stat().st_size, sha256=digest, exchange_checksum=bool(expected))
    log(f"archive ready {symbol} {day} {result['bytes'] / 1e6:.1f} MB")
    return result


def load_frame(path, symbol, multiplier):
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as stream:
            if symbol.startswith("BINANCE."):
                frame = pd.read_csv(stream, usecols=["agg_trade_id", "price", "quantity", "transact_time", "is_buyer_maker"])
                frame = frame.rename(columns={"agg_trade_id": "id", "quantity": "qty", "transact_time": "ms", "is_buyer_maker": "sell"})
                if frame.sell.dtype != bool:
                    values = frame.sell.astype(str).str.lower()
                    if not values.isin(["true", "false"]).all():
                        raise ValueError("Invalid Binance side")
                    frame["sell"] = values.eq("true")
            else:
                frame = pd.read_csv(stream, usecols=["trade_id", "side", "price", "size", "created_time"])
                if not frame.side.isin(["buy", "sell"]).all():
                    raise ValueError("Invalid OKX side")
                frame = frame.rename(columns={"trade_id": "id", "size": "qty", "created_time": "ms"})
                frame["sell"] = frame.side.eq("sell")
                frame["qty"] = frame.qty * multiplier
    frame = frame[["id", "price", "qty", "ms", "sell"]].sort_values("id", kind="stable", ignore_index=True)
    if frame.empty or frame.id.duplicated().any():
        raise ValueError(f"Empty archive or duplicate IDs: {path}")
    if not np.isfinite(frame[["price", "qty"]].to_numpy()).all() or (frame[["price", "qty"]] <= 0).any().any():
        raise ValueError(f"Invalid price/quantity: {path}")
    return frame


class AuditDetector(LargeTradeDetector):
    """Identical streaming decisions, retaining just the current event plus baseline."""

    def __init__(self, symbol, settings):
        source = "aggregate" if symbol.startswith("BINANCE.") else "trade"
        super().__init__(symbol, source, settings)
        self.records = []
        self.threshold = math.nan
        self.baseline_n = 0
        self.mean = math.nan
        self.prints = 0
        self.last_price = math.nan
        self.last_id = None
        self.recorded = None

    def _settle(self):
        # Retention, CVD and chart coverage are irrelevant to classification.
        # Baseline eviction still runs in the unmodified _process().
        pass

    def _judge(self, key, ns, price, qty, side):
        super()._judge(key, ns, price, qty, side)
        n = len(self._window)
        position = (n - 1) * self.settings["quantile"]
        lo, hi = math.floor(position), math.ceil(position)
        self.threshold = self._sorted[lo] + (self._sorted[hi] - self._sorted[lo]) * (position - lo)
        self.baseline_n = n
        self.mean = self._sum / n

    def finish(self):
        if not self.raw:
            return
        key = next(iter(self.raw))
        if key == self.recorded:
            return
        ns, price, qty, side, reset, usable, joined = self.raw[key]
        ready = self.readiness[key][1]
        notional = price * qty
        if ready:
            fixed = notional >= self.settings["minNotional"]
            relative = qty > self.threshold
            category = 2 if fixed and relative else 3 if relative else 4 if fixed else 1
            if (key in self.events) != (category == 2):
                raise AssertionError("Audit differs from live _judge")
        else:
            category = 0
        self.records.append((key, self.last_id, ns // 1_000_000, side, qty, price,
                             self.last_price, notional, self.threshold, price * self.threshold,
                             self.mean, self.baseline_n, self.prints, category, reset))
        self.recorded = key

    def _append(self, key, row):
        self.finish()
        self.raw.clear()
        self.events.clear()
        self.readiness.clear()
        self.threshold = self.mean = math.nan
        self.baseline_n = 0
        self.prints = 1
        super()._append(key, row)

    def _extend(self, key, price, qty):
        self.prints += 1
        super()._extend(key, price, qty)

    def trade(self, identity, price, qty, ms, sell):
        super().trade(identity, price, qty, ms, sell)
        self.last_id, self.last_price = int(identity), float(price)


def verify_against_live(frame, symbol, settings):
    """Compare unmodified production storage with the audit on actual raw prints."""
    frame = frame.iloc[:100_000]
    source = "aggregate" if symbol.startswith("BINANCE.") else "trade"
    live = LargeTradeDetector(symbol, source, settings)
    audit = AuditDetector(symbol, settings)
    for row in frame.itertuples(index=False, name=None):
        live.trade(*row)
        audit.trade(*row)
    audit.finish()
    accepted = {row[0]: row for row in audit.records if row[13] == 2}
    if set(accepted) != set(live.events):
        raise AssertionError(f"Live/audit accepted event IDs differ: {symbol}")
    for key, event in live.events.items():
        row = accepted[key]
        if row[4] != event["qty"] or row[5] != event["price"] or row[8] != event["threshold"]:
            raise AssertionError(f"Live/audit event values differ: {symbol} {key}")
    if audit._sorted != live._sorted or list(audit._lagged) != list(live._lagged):
        raise AssertionError("Live/audit final baselines differ")
    result = dict(raw_prints=len(frame), merged_events=len(audit.records), accepted_events=len(accepted), exact_match=True)
    log(f"validated {symbol}: {result}")
    return result


def replay_symbol(symbol, files, multiplier, start_ms, end_ms, output):
    settings = settings_for(symbol, "aggregate" if symbol.startswith("BINANCE.") else "trade")
    if settings["minQty"] != 0:
        raise ValueError("This two-gate audit requires minQty=0; a third active gate needs its own category.")
    detector = AuditDetector(symbol, settings)
    pieces, quality, previous_id, previous_ms, validation = [], [], None, None, None
    raw_total = 0
    for item in sorted(files, key=lambda x: x["day"]):
        frame = load_frame(item["file"], symbol, multiplier)
        shift = 0 if symbol.startswith("BINANCE.") else 8 * 3_600_000
        day_ms = int(datetime.fromisoformat(item["day"]).replace(tzinfo=UTC).timestamp() * 1000) - shift
        if frame.ms.min() < day_ms or frame.ms.max() >= day_ms + DAY_MS:
            raise ValueError(f"Archive date does not match timestamps: {item['file']}")
        frame = frame[(frame.ms >= start_ms - 30 * 60_000) & (frame.ms < end_ms)]
        if frame.empty:
            continue
        ids, times = frame.id.to_numpy(), frame.ms.to_numpy()
        id_diff, time_diff = np.diff(ids), np.diff(times)
        if previous_id is not None:
            id_diff = np.r_[ids[0] - previous_id, id_diff]
            time_diff = np.r_[times[0] - previous_ms, time_diff]
        if (id_diff <= 0).any():
            raise ValueError(f"Overlapping archive IDs: {symbol}")
        quality.append(dict(day=item["day"], raw_prints=len(frame),
                            missing_ids=int(np.maximum(id_diff - 1, 0).sum()),
                            gap_locations=int((id_diff != 1).sum()), backward_timestamps=int((time_diff < 0).sum()),
                            first_id=int(ids[0]), last_id=int(ids[-1]), first_ms=int(times[0]), last_ms=int(times[-1])))
        if validation is None and len(frame) >= 20_000:
            validation = verify_against_live(frame, symbol, settings)
        began = time.monotonic()
        for i, row in enumerate(frame.itertuples(index=False, name=None), 1):
            detector.trade(*row)
            if i % 500_000 == 0:
                log(f"replay {symbol} {item['day']} {i:,}/{len(frame):,} raw prints")
        # Archives have disjoint millisecond ranges, so their final event is complete.
        detector.finish()
        pieces.append(np.array(detector.records, dtype=DTYPE))
        detector.records.clear()
        previous_id, previous_ms = int(ids[-1]), int(times[-1])
        raw_total += len(frame)
        log(f"replayed {symbol} {item['day']}: {len(frame):,} prints / {len(pieces[-1]):,} events in {time.monotonic()-began:.1f}s")
    if not pieces or validation is None:
        raise ValueError(f"Insufficient data for replay/validation: {symbol}")
    events = np.concatenate(pieces)
    if (np.diff(events["ms"]) < 0).any():
        raise ValueError("Markout analysis requires monotonic event timestamps")
    np.savez_compressed(output / f"{symbol}.events.npz", events=events)
    meta = dict(symbol=symbol, settings=settings, multiplier=multiplier,
                raw_prints_with_warmup=raw_total, quality=quality, validation=validation)
    (output / f"{symbol}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return events, meta


def markouts(events, seconds):
    times = events["ms"]
    targets = times + seconds * 1000
    index = np.searchsorted(times, targets, side="right") - 1
    resets = np.cumsum(events["reset"])
    valid = (targets <= times[-1]) & (targets - times[index] <= 5_000) & (resets[index] == resets)
    value = (events["last_price"][index] / events["last_price"] - 1) * events["side"] * 10_000
    return np.where(valid, value, np.nan)


def stat_row(symbol, label, records):
    counts = {code: int((records["category"] == code).sum()) for code in LABELS}
    amounts = {code: float(records["notional"][records["category"] == code].sum()) for code in LABELS}
    floor_candidates, relative_candidates = counts[2] + counts[4], counts[2] + counts[3]
    row = dict(symbol=symbol, period=label, events=len(records), raw_prints=int(records["prints"].sum()),
               warmup=counts[0], neither=counts[1], accepted=counts[2], fixed_blocked=counts[3], p99_blocked=counts[4],
               accepted_usdt=amounts[2], fixed_blocked_usdt=amounts[3], p99_blocked_usdt=amounts[4],
               neither_usdt=amounts[1], warmup_usdt=amounts[0],
               fixed_block_rate=counts[3] / relative_candidates if relative_candidates else None,
               p99_block_rate=counts[4] / floor_candidates if floor_candidates else None,
               fixed_block_amount_rate=amounts[3] / (amounts[2] + amounts[3]) if amounts[2] + amounts[3] else None,
               p99_block_amount_rate=amounts[4] / (amounts[2] + amounts[4]) if amounts[2] + amounts[4] else None)
    assert sum(counts.values()) == len(records)
    return row


def summarize(symbol, events, meta, start_ms, end_ms, output):
    returns = {s: markouts(events, s) for s in (5, 30, 60, 300)}
    target = (events["ms"] >= start_ms) & (events["ms"] < end_ms)
    data = events[target]
    result = dict(meta)
    result["summary"] = stat_row(symbol, "all", data)
    result["daily"] = [stat_row(symbol, datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d"),
                                data[(data["ms"] >= ms) & (data["ms"] < ms + DAY_MS)])
                       for ms in range(start_ms, end_ms, DAY_MS)]
    result["hourly"] = [stat_row(symbol, datetime.fromtimestamp(ms / 1000, BJ).isoformat(),
                                 data[(data["ms"] >= ms) & (data["ms"] < ms + 3_600_000)])
                        for ms in range(start_ms, end_ms, 3_600_000)]
    result["groups"] = []
    floor = meta["settings"]["minNotional"]
    for code in (2, 3, 4):
        selected = data["category"] == code
        subset = data[selected]
        group = dict(category=LABELS[code], code=code, count=len(subset),
                     buy_count=int((subset["side"] == 1).sum()), sell_count=int((subset["side"] == -1).sum()),
                     buy_usdt=float(subset["notional"][subset["side"] == 1].sum()),
                     sell_usdt=float(subset["notional"][subset["side"] == -1].sum()),
                     amount_quantiles={str(q): float(np.quantile(subset["notional"], q)) for q in (0, .25, .5, .75, .9, .99, 1)} if len(subset) else {},
                     over_2x_floor=int((subset["notional"] >= floor * 2).sum()),
                     over_5x_floor=int((subset["notional"] >= floor * 5).sum()),
                     markouts={})
        for seconds, values in returns.items():
            values = values[target][selected]
            values = values[np.isfinite(values)]
            group["markouts"][str(seconds)] = dict(n=len(values), mean_bps=float(values.mean()) if len(values) else None,
                                                      median_bps=float(np.median(values)) if len(values) else None,
                                                      positive_rate=float((values > 0).mean()) if len(values) else None,
                                                      median_abs_bps=float(np.median(abs(values))) if len(values) else None)
        result["groups"].append(group)
    candidate = data["category"] >= 2
    table = pd.DataFrame.from_records(data[candidate])
    table.insert(0, "symbol", symbol)
    table.insert(3, "time_beijing", pd.to_datetime(table.ms, unit="ms", utc=True).dt.tz_convert(BJ).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3])
    table["reason"] = table.category.map(LABELS)
    table["fixed_floor_usdt"] = floor
    table["qty_over_p99"] = table.qty / table.p99_qty
    for seconds, values in returns.items():
        table[f"signed_return_{seconds}s_bps"] = values[target][candidate]
    table.to_csv(output / f"{symbol}.candidates.csv.gz", index=False, compression="gzip", float_format="%.12g")
    top = pd.concat([table[table.category == code].nlargest(20, "notional") for code in (2, 3, 4)])
    top.to_csv(output / f"{symbol}.top_events.csv", index=False, encoding="utf-8-sig", float_format="%.12g")
    result["top_events"] = json.loads(top.replace({np.nan: None}).to_json(orient="records"))
    threshold = data["p99_notional"][data["category"] != 0]
    result["p99_notional_quantiles"] = {str(q): float(np.quantile(threshold, q)) for q in (.01, .1, .5, .9, .99)}
    result["p99_above_fixed_fraction"] = float((threshold > floor).mean())
    result["target_first_ms"], result["target_last_ms"] = int(data["ms"][0]), int(data["ms"][-1])
    (output / f"{symbol}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    log(f"RESULT {symbol}: accepted={result['summary']['accepted']:,} fixed_blocked={result['summary']['fixed_blocked']:,} p99_blocked={result['summary']['p99_blocked']:,}")
    return result


def enrich_result(result, events, start_ms, end_ms, output):
    """Measure the actual per-candle marker consequences, not just event counts."""
    if int(events["prints"].sum()) != result["raw_prints_with_warmup"]:
        raise AssertionError("Raw prints were lost or counted twice")
    if not np.array_equal(events["last_id"] - events["id"] + 1, events["prints"]):
        raise AssertionError("Merged-event IDs do not match print counts")
    target = (events["ms"] >= start_ms) & (events["ms"] < end_ms)
    data = events[target]
    accepted = data["category"] == 2
    result["marker_impact"] = []
    for tf in (10, 30, 60, 300):
        keys = (data["ms"] // (tf * 1000)) * 2 + (data["side"] == 1)
        existing = np.unique(keys[accepted])
        for code in (3, 4):
            mask = data["category"] == code
            already = np.isin(keys[mask], existing)
            new_markers = np.setdiff1d(np.unique(keys[mask]), existing).size
            notionals = data["notional"][mask]
            result["marker_impact"].append(dict(symbol=result["symbol"], tf_seconds=tf,
                gate_removed="金额门槛" if code == 3 else "P99", category=code,
                existing_markers=len(existing), added_markers=int(new_markers),
                added_marker_ratio=new_markers / len(existing) if len(existing) else None,
                blocked_events_on_existing_marker_rate=float(already.mean()) if len(already) else None,
                blocked_amount_on_existing_marker_rate=float(notionals[already].sum() / notionals.sum()) if notionals.sum() else None))
    for group in result["groups"]:
        rows = data[data["category"] == group["code"]]
        ratios = rows["qty"] / rows["p99_qty"]
        group["qty_over_p99_quantiles"] = {str(q): float(np.quantile(ratios, q)) for q in (.1, .5, .9, 1)} if len(rows) else {}
    relative = data[data["category"] == 3]
    order = np.argsort(relative["qty"] / relative["p99_qty"])[-10:][::-1]
    result["most_relative_blocked"] = [dict(id=int(row["id"]), last_id=int(row["last_id"]),
        time_beijing=datetime.fromtimestamp(int(row["ms"]) / 1000, BJ).isoformat(timespec="milliseconds"),
        side=int(row["side"]), notional=float(row["notional"]), p99_notional=float(row["p99_notional"]),
        qty_over_p99=float(row["qty"] / row["p99_qty"])) for row in relative[order]]
    frame = pd.DataFrame.from_records(data[data["category"] >= 2])
    frame["minute_ms"] = frame.ms // 60_000 * 60_000
    existing = frame[frame.category == 2].groupby(["minute_ms", "side"]).agg(
        accepted_events=("id", "size"), accepted_usdt=("notional", "sum"))
    clusters = []
    for code in (3, 4):
        grouped = frame[frame.category == code].groupby(["minute_ms", "side"]).agg(
            blocked_events=("id", "size"), blocked_usdt=("notional", "sum"), largest_blocked_usdt=("notional", "max"))
        grouped = grouped.join(existing, how="left").fillna(0).nlargest(10, "blocked_usdt").reset_index()
        grouped["reason"] = LABELS[code]
        grouped["symbol"] = result["symbol"]
        grouped["time_beijing"] = pd.to_datetime(grouped.minute_ms, unit="ms", utc=True).dt.tz_convert(BJ).dt.strftime("%Y-%m-%d %H:%M")
        clusters.extend(json.loads(grouped.to_json(orient="records")))
    result["largest_blocked_minutes"] = clusters
    result["validation"]["all_prints_accounted_for"] = True
    result["validation"]["all_merged_id_ranges_match"] = True
    (output / f"{result['symbol']}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


def write_report(results, manifest, output):
    # A self-contained HTML report is useful both in Codex and any regular browser.
    summary = pd.DataFrame([r["summary"] | {"floor_usdt": r["settings"]["minNotional"]} for r in results])
    daily = pd.DataFrame([d for r in results for d in r["daily"]])
    hourly = pd.DataFrame([d for r in results for d in r["hourly"]])
    summary.to_csv(output / "summary.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(output / "daily.csv", index=False, encoding="utf-8-sig")
    hourly.to_csv(output / "hourly.csv", index=False, encoding="utf-8-sig")
    markers = pd.DataFrame([row for result in results for row in result.get("marker_impact", [])])
    clusters = pd.DataFrame([row for result in results for row in result.get("largest_blocked_minutes", [])])
    if not markers.empty:
        markers.to_csv(output / "marker_impact.csv", index=False, encoding="utf-8-sig")
        clusters.to_csv(output / "blocked_minutes.csv", index=False, encoding="utf-8-sig")
    manifest["detector_sha256"] = hashlib.sha256((ROOT / "large_trades.py").read_bytes()).hexdigest()
    manifest["defaults_sha256"] = hashlib.sha256((ROOT / "large_trade_defaults.py").read_bytes()).hexdigest()
    manifest["research_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    all_result = dict(manifest=manifest, results=results)
    (output / "results.json").write_text(json.dumps(all_result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    body = ["<h1>加密大额成交：金额门槛与 P99 回放审计</h1>",
            f"<p>{html.escape(manifest['period_beijing'])} · {len(results)} 个交易所合约 · 全量原始成交逐笔回放</p>",
            "<p>复用线上检测器的成交合并、10 分钟基线、30 秒滞后、200 事件预热和 P99 插值。开始前额外输入 30 分钟数据；跨文件保持基线，成交漏号则按线上规则重新预热。金额线保持当前配置，不调整。</p>",
            "<p><b>仅金额拦截</b>：通过 P99、未达到金额线；拦截率分母为全部通过 P99 的事件。<b>仅 P99 拦截</b>：达到金额线、未通过 P99；拦截率分母为全部达到金额线的可判定事件。两类互斥。预热单独列出。</p>",
            "<p>默认金额线由 2026-10-14 归档标定，该日不是独立验证样本；本次是用现有配置回看历史，不是当时可执行的策略收益回测。北京时间与 UTC 日期已明确区分。</p>"]
    totals = summary.select_dtypes(include="number").sum()
    fixed_count_rate = totals.fixed_blocked / (totals.accepted + totals.fixed_blocked)
    p99_count_rate = totals.p99_blocked / (totals.accepted + totals.p99_blocked)
    fixed_amount_rate = totals.fixed_blocked_usdt / (totals.accepted_usdt + totals.fixed_blocked_usdt)
    p99_amount_rate = totals.p99_blocked_usdt / (totals.accepted_usdt + totals.p99_blocked_usdt)
    double_count = sum(g["over_2x_floor"] for r in results for g in r["groups"] if g["code"] == 4)
    five_count = sum(g["over_5x_floor"] for r in results for g in r["groups"] if g["code"] == 4)
    body.append(f"<h2>主要发现</h2><p>正式区间共 {int(totals.raw_prints):,} 条原始成交，合并为 {int(totals.events):,} 个事件；{int(totals.accepted):,} 个同时通过两道门槛。金额线额外拦截 {int(totals.fixed_blocked):,} 个，占 P99 候选事件的 {fixed_count_rate:.1%}、候选成交额的 {fixed_amount_rate:.1%}。P99 额外拦截 {int(totals.p99_blocked):,} 个，占金额达标候选事件的 {p99_count_rate:.1%}、候选成交额的 {p99_amount_rate:.1%}。</p>")
    body.append(f"<p>P99 拦截的事件中，只有 {double_count:,} 个达到各自固定金额线的两倍，达到五倍的有 {five_count:,} 个。样本内存在大额漏标，但被拦截的主要是门槛附近事件。固定金额线也会挡住局部异常，具体例子与原始成交编号在下方。</p>")
    body.append("<p><b>本次判断：</b>暂不取消 P99，不修改线上参数。更值得优先研究固定金额硬门槛对局部异常的过滤，并将‘绝对规模’与‘相对异常程度’分层展示。被挡事件的后续价格变化在币种、方向和时段之间并不一致，当前回放不能证明放宽门槛会改善交易效果。</p>")
    table = summary[["symbol", "floor_usdt", "accepted", "fixed_blocked", "fixed_block_rate", "p99_blocked", "p99_block_rate"]].copy()
    for column in ("fixed_block_rate", "p99_block_rate"):
        table[column] = table[column].map(lambda v: f"{v:.1%}")
    table.columns = ["合约", "固定金额线 USDT", "两项通过", "仅金额拦截", "金额拦截率", "仅P99拦截", "P99拦截率"]
    body.extend(["<h2>三天总览</h2>", table.to_html(index=False, border=0)])
    amounts = summary[["symbol", "accepted_usdt", "fixed_blocked_usdt", "fixed_block_amount_rate", "p99_blocked_usdt", "p99_block_amount_rate"]].copy()
    for column in ("accepted_usdt", "fixed_blocked_usdt", "p99_blocked_usdt"):
        amounts[column] = (amounts[column] / 1e6).round(2)
    for column in ("fixed_block_amount_rate", "p99_block_amount_rate"):
        amounts[column] = amounts[column].map(lambda v: f"{v:.1%}")
    amounts.columns = ["合约", "通过金额（百万USDT）", "金额线拦截金额（百万）", "占P99候选金额", "P99拦截金额（百万）", "占金额达标候选金额"]
    body.append(amounts.to_html(index=False, border=0))
    if not markers.empty:
        body.append("<h2>对图上圆点的实际影响</h2><p>按同一根 K 线、同一买卖方向检查。已有圆点的事件被拦截，会影响圆点量和 Delta/CVD；只有该侧原本完全没有合格事件时，放开门槛才会新增圆点。下表用30秒周期，CSV还含10秒、1分钟、5分钟。多所汇总图可能有其他交易所的圆点，本表为单所统计。</p>")
        marker_table = markers[markers.tf_seconds == 30][["symbol", "gate_removed", "existing_markers", "added_markers", "blocked_events_on_existing_marker_rate", "blocked_amount_on_existing_marker_rate"]].copy()
        for column in ("blocked_events_on_existing_marker_rate", "blocked_amount_on_existing_marker_rate"):
            marker_table[column] = marker_table[column].map(lambda v: f"{v:.1%}")
        marker_table.columns = ["合约", "放开的门槛", "现有圆点", "会新增圆点", "被挡笔数已有同向圆点", "被挡金额已有同向圆点"]
        body.append(marker_table.to_html(index=False, border=0))
    body.append("<h2>逐合约明细</h2>")
    for result in results:
        symbol = result["symbol"]
        body.append(f"<h3>{html.escape(symbol)}</h3>")
        rows = []
        for g in result["groups"]:
            q, m = g["amount_quantiles"], g["markouts"]["60"]
            rows.append({"类别": g["category"], "事件": g["count"], "金额合计 USDT": round(g["buy_usdt"]+g["sell_usdt"]),
                         "金额中位数": round(q.get("0.5", 0)), "最大金额": round(q.get("1", 0)),
                         "≥2倍固定线": g["over_2x_floor"], "≥5倍固定线": g["over_5x_floor"],
                         "60秒同向变化均值 bp": round(m["mean_bps"], 3) if m["mean_bps"] is not None else None,
                         "60秒同向变化中位数 bp": round(m["median_bps"], 3) if m["median_bps"] is not None else None})
        body.append(pd.DataFrame(rows).to_html(index=False, border=0))
        day_table = pd.DataFrame(result["daily"])[["period", "accepted", "fixed_blocked", "fixed_block_rate", "p99_blocked", "p99_block_rate"]]
        body.append("<details><summary>按 UTC 日拆分（北京时间当天 08:00 至次日 08:00）</summary>"+day_table.to_html(index=False, border=0)+"</details>")
        for code in (3, 4):
            top = pd.DataFrame([row for row in result["top_events"] if row["category"] == code]).head(5)
            if top.empty:
                continue
            top = top[["time_beijing", "side", "notional", "p99_notional", "qty_over_p99", "signed_return_60s_bps", "id", "last_id"]].copy()
            top["side"] = top.side.map({1: "主动买", -1: "主动卖"})
            for c in ("notional", "p99_notional"):
                top[c] = top[c].round(2)
            top.columns = ["北京时间", "方向", "成交金额 USDT", "当时P99折算 USDT", "数量/P99", "60秒同向变化 bp", "首编号", "末编号"]
            body.append(f"<h4>{LABELS[code]}：金额最大的 5 个事件</h4>"+top.to_html(index=False, border=0))
        if result.get("most_relative_blocked"):
            relative = pd.DataFrame(result["most_relative_blocked"][:3])[["time_beijing", "side", "notional", "p99_notional", "qty_over_p99"]]
            relative["side"] = relative.side.map({1: "主动买", -1: "主动卖"})
            relative.columns = ["北京时间", "方向", "被金额线挡住的金额", "当时P99折算金额", "数量/P99"]
            body.append("<details><summary>被金额线拦截，但相对 P99 倍数最高的 3 笔</summary>"+relative.round(3).to_html(index=False, border=0)+"</details>")
        body.append(f'<p><a href="{symbol}.top_events.csv">每类最大20笔</a> · <a href="{symbol}.candidates.csv.gz">全部候选事件（CSV gzip）</a></p>')
    body.append("<h2>数据质量与解释边界</h2><p>币安 ZIP 对交易所 SHA256 校验；两所 CSV 校验 ZIP CRC、日期、重复编号、缺号、正价格正数量。OKX 张数乘公开合约面值折为币。归档合约面值是本次查询值；未有历史面值变更证据时按当前面值处理。</p>")
    quality = [{"合约": r["symbol"], "回放原始条数含预热": r["raw_prints_with_warmup"],
                "缺失编号": sum(d["missing_ids"] for d in r["quality"]),
                "时间倒退": sum(d["backward_timestamps"] for d in r["quality"]),
                "与原检测器逐项完全一致的验证条数": r["validation"]["raw_prints"],
                "正式区间预热事件": r["summary"]["warmup"]} for r in results]
    body.append(pd.DataFrame(quality).to_html(index=False, border=0))
    body.append("<p>后续变化取事件最后成交价至目标时刻最近已发生的成交价，乘主动方向，以基点表示（1bp=0.01%）；未来端不足、跨缺号或目标成交价陈旧超过5秒的样本排除。事件可能密集重叠，未控制行情状态、未扣手续费/滑点，不能把均值当成独立预测能力或交易收益。盘口归档未纳入，无法据此确认扫单深度或吸收。</p>")
    body.append('<p><a href="summary.csv">总览CSV</a> · <a href="daily.csv">分日CSV</a> · <a href="hourly.csv">分时CSV</a> · <a href="marker_impact.csv">圆点影响CSV</a> · <a href="blocked_minutes.csv">被挡大单聚集时段CSV</a> · <a href="manifest.json">数据来源与SHA256</a> · <a href="results.json">完整统计JSON</a></p>')
    document = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>加密大额成交回放审计</title><style>body{font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif;max-width:1450px;margin:32px auto;padding:0 24px;color:#152238;background:#f6f8fb}h1,h2,h3{color:#153c65}h2{margin-top:38px}h3{margin-top:32px}table{border-collapse:collapse;width:100%;font-size:13px;background:white;margin:16px 0;display:block;overflow-x:auto}th,td{padding:9px 12px;border-bottom:1px solid #dce3eb;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{background:#e7eff7}a{color:#075eb6}details{padding:8px;background:#eef2f7}p{max-width:1150px}</style><body>" + "\n".join(body) + "</body></html>"
    (output / "report.html").write_text(document, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-10-13")
    parser.add_argument("--end", default="2026-10-16")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--cache", type=Path, default=ROOT / ".run-tmp" / "crypto-large-replay" / "archives")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "research" / "crypto-large-replay-2026-10-17")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--reuse-replay", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    symbols = args.symbols or sorted(s for s in MIN_NOTIONAL if s.startswith(("BINANCE.", "OKX.")))
    start_ms, end_ms = [int(datetime.fromisoformat(x).replace(tzinfo=UTC).timestamp() * 1000) for x in (args.start, args.end)]
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    previous_manifest = None
    if args.reuse_replay:
        path = args.output / "manifest.json"
        if not path.exists():
            raise ValueError("--reuse-replay requires an existing audited manifest")
        previous_manifest = json.loads(path.read_text(encoding="utf-8"))
        if (previous_manifest["start_ms"], previous_manifest["end_ms"]) != (start_ms, end_ms):
            raise ValueError("Cached replay date range differs; use a new output directory")
        if previous_manifest.get("detector_sha256") != hashlib.sha256((ROOT / "large_trades.py").read_bytes()).hexdigest():
            raise ValueError("Detector changed; replay raw trades again without --reuse-replay")
    info_path = args.cache / "okx_instruments.json"
    if not info_path.exists():
        info_path.write_bytes(request_bytes("https://www.okx.com/api/v5/public/instruments?instType=SWAP"))
    info = json.loads(info_path.read_bytes())
    instruments = {f"OKX.{i['instId']}": i for i in info["data"]}
    multipliers = {s: 1.0 if s.startswith("BINANCE.") else float(instruments[s]["ctVal"]) for s in symbols}
    tasks = [(s, d) for s in symbols for d in dates_for(s, start_ms - 30 * 60_000, end_ms)]
    archives, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(download_one, args.cache, s, d): (s, d) for s, d in tasks}
        for future in as_completed(futures):
            try:
                archives.append(future.result())
            except Exception as exc:
                errors.append(dict(symbol=futures[future][0], day=futures[future][1], error=str(exc)))
                log(f"DOWNLOAD ERROR {errors[-1]}")
    manifest = dict(period_beijing=f"{datetime.fromtimestamp(start_ms/1000,BJ):%Y-%m-%d %H:%M} 至 {datetime.fromtimestamp(end_ms/1000,BJ):%Y-%m-%d %H:%M}（右端不含）",
                    start_ms=start_ms, end_ms=end_ms, generated_at=datetime.now(BJ).isoformat(),
                    archive_files=sorted(archives, key=lambda r: (r["symbol"], r["day"])), errors=errors,
                    okx_contracts={s: {k: instruments[s].get(k) for k in ("instId", "ctVal", "ctMult", "ctValCcy", "listTime")} for s in symbols if s.startswith("OKX.")})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    if errors:
        raise SystemExit("Incomplete archives; see manifest.json. No partial result silently accepted.")
    if previous_manifest:
        old_hashes = {(a["symbol"], a["day"]): a["sha256"] for a in previous_manifest["archive_files"]}
        if any(old_hashes.get((a["symbol"], a["day"])) != a["sha256"] for a in archives):
            raise ValueError("Archive input changed; replay raw trades again")
    if args.download_only:
        return
    results = []
    for symbol in symbols:
        cached = args.output / f"{symbol}.events.npz"
        if args.reuse_replay and cached.exists():
            events = np.load(cached)["events"]
            meta = json.loads((args.output / f"{symbol}.meta.json").read_text(encoding="utf-8"))
            current = settings_for(symbol, "aggregate" if symbol.startswith("BINANCE.") else "trade")
            if meta["settings"] != current or meta["multiplier"] != multipliers[symbol]:
                raise ValueError("Settings or contract multiplier changed; replay raw trades again")
        else:
            events, meta = replay_symbol(symbol, [a for a in archives if a["symbol"] == symbol], multipliers[symbol], start_ms, end_ms, args.output)
        result = summarize(symbol, events, meta, start_ms, end_ms, args.output)
        results.append(enrich_result(result, events, start_ms, end_ms, args.output))
    write_report(results, manifest, args.output)
    log(f"REPORT {args.output / 'report.html'}")


if __name__ == "__main__":
    main()
