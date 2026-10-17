"""Build an offline two-tier crypto replay preview; never alter live settings.

Reuses the audited, completed P99 events from crypto_large_trade_replay.py.
Candlesticks are rebuilt from the original exchange prints, not event averages.
Run: .venv/Scripts/python.exe docs/crypto_large_tier_preview.py --visual-output PATH
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docs.crypto_large_trade_replay import BJ, load_frame, log  # noqa: E402
from docs.crypto_large_floor_compare import size_unit, radius, validate_renderer  # noqa: E402
from large_trades import settings_for  # noqa: E402

TF_MS = 30_000


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def aggregate_events(events, start, end, floor):
    """Two mutually exclusive event classes, each aggregated by bar and side."""
    n = (end - start) // TF_MS
    target = (events['ms'] >= start) & (events['ms'] < end)
    events = events[target]
    passed = (events['category'] != 0) & (events['qty'] > events['p99_qty'])
    large = passed & (events['notional'] >= floor)
    relative = passed & (events['notional'] < floor)
    assert np.array_equal(large, events['category'] == 2)
    assert np.array_equal(relative, events['category'] == 3)
    assert not (large & relative).any()
    assert np.array_equal(large | relative, passed)
    keys = ((events['ms'] - start) // TF_MS) * 2 + (events['side'] == 1)
    layers = []
    for mask in (large, relative):
        counts = np.bincount(keys[mask], minlength=n * 2).reshape(n, 2)
        qty = np.bincount(keys[mask], weights=events['qty'][mask], minlength=n * 2).reshape(n, 2)
        money = np.bincount(keys[mask], weights=events['notional'][mask], minlength=n * 2).reshape(n, 2)
        layers.append(dict(count=counts, qty=qty, notional=money))
    return layers, int(events['prints'].sum())


def raw_candles(symbol, meta, manifest, output, before_price):
    """Exact OHLC from raw prints. Cache is keyed by every source archive hash."""
    sources = [s for s in manifest['archive_files'] if s['symbol'] == symbol]
    signature = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    path = output / f'{symbol}.ohlc.npz'
    if path.exists():
        with np.load(path) as cache:
            if cache['signature'].item() == signature:
                return cache['ohlc'], cache['print_count'], signature
    start, end = manifest['start_ms'], manifest['end_ms']
    n = (end - start) // TF_MS
    candles = np.full((n, 4), np.nan)
    prints = np.zeros(n, dtype='int64')
    for source in sorted(sources, key=lambda s: s['day']):
        # Binance's extra UTC day was only used for detector warm-up.
        if symbol.startswith('BINANCE.') and source['day'] < '2026-10-13':
            continue
        if digest(source['file']) != source['sha256']:
            raise ValueError(f"Archive changed: {source['file']}")
        frame = load_frame(source['file'], symbol, meta['multiplier'])
        frame = frame[(frame.ms >= start) & (frame.ms < end)]
        if frame.empty:
            continue
        assert (np.diff(frame.ms.to_numpy()) >= 0).all()
        grouped = frame.groupby((frame.ms - start) // TF_MS, sort=True).price
        values = grouped.agg(['first', 'max', 'min', 'last', 'count'])
        indices = values.index.to_numpy(dtype='int64')
        assert not prints[indices].any(), 'Overlapping archive bar ranges'
        candles[indices] = values[['first', 'max', 'min', 'last']].to_numpy()
        prints[indices] = values['count'].to_numpy(dtype='int64')
    previous = before_price
    for i in range(n):
        if not prints[i]:
            candles[i] = previous
        previous = candles[i, 3]
    assert np.isfinite(candles).all()
    assert (candles[:, 1] >= candles[:, [0, 3]].max(axis=1)).all()
    assert (candles[:, 2] <= candles[:, [0, 3]].min(axis=1)).all()
    np.savez_compressed(path, signature=signature, ohlc=candles, print_count=prints)
    return candles, prints, signature


def summary(name, layers):
    large, relative = layers
    p = large['count'] > 0
    r = relative['count'] > 0
    return dict(view=name, large_events=int(large['count'].sum()), relative_events=int(relative['count'].sum()),
        large_markers=int(p.sum()), relative_markers=int(r.sum()),
        relative_only_locations=int((r & ~p).sum()), both_layers_locations=int((r & p).sum()),
        large_notional_usdt=float(large['notional'].sum()), relative_notional_usdt=float(relative['notional'].sum()))


def preview(name, layers, candles, prints, floor, start):
    """Typical and densest 30-minute samples, selected on the final UTC day."""
    n = len(candles)
    candidates = [(int((layers[1]['count'][stop-60:stop] > 0).sum()), stop)
                  for stop in range(n-2880+60, n+1, 60)]
    candidates.sort()
    windows, checks = [], []
    for key, label, (_, stop) in [('typical', '日内居中', candidates[len(candidates)//2]),
                                 ('dense', '日内最密集', candidates[-1])]:
        begin = stop - 60
        units = []
        for cursor in range(begin + 1, stop + 1):
            # Same unit for both layers; relative events never enter its P90.
            q = layers[0]['qty'][max(0, cursor-800):cursor].round(8)
            unit = size_unit(q, floor / candles[cursor-1, 3])
            units.append(float(unit))
            if cursor in (begin + 1, stop):
                checks.append(dict(view=name, min_floor=floor, reference=float(candles[cursor-1, 3]),
                    qty=q.tolist(), unit=float(unit), radii=radius(q, unit).tolist()))
        rows = []
        for i in range(begin, stop):
            row = [float(v) for v in candles[i]]
            for field in ('qty', 'count', 'notional'):
                for layer in layers:
                    for value in layer[field][i]:
                        row.append(int(value) if field == 'count' else round(float(value), 8 if field == 'qty' else 4))
            rows.append(row)
        windows.append(dict(key=key, label=label,
            start_ms=start+begin*TF_MS,
            date=datetime.fromtimestamp((start+begin*TF_MS)/1000, BJ).strftime('%m-%d %H:%M'),
            unit=units, rows=rows, raw_prints=prints[begin:stop].astype(int).tolist(),
            summary=summary(name, [{k:v[begin:stop] for k,v in layer.items()} for layer in layers])))
    return windows, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'data/research/crypto-large-replay-2026-10-17')
    parser.add_argument('--visual-output', type=Path, required=True)
    args = parser.parse_args()
    output = args.input / 'two-tier-preview'
    output.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    manifest = json.loads((args.input / 'manifest.json').read_text(encoding='utf-8'))
    old = json.loads((args.input / 'floor-comparison/results.json').read_text(encoding='utf-8'))
    for file, expected in [('large_trades.py', manifest['detector_sha256']),
                           ('large_trade_defaults.py', manifest['defaults_sha256']),
                           ('static/large-trades.js', old['renderer_sha256'])]:
        assert digest(ROOT / file) == expected, f'Live source changed: {file}'
    start, end = manifest['start_ms'], manifest['end_ms']
    all_layers, all_candles, all_prints, floors, metadata = {}, {}, {}, {}, []
    for source in old['provenance']:
        symbol = source['symbol']
        assert digest(source['events_file']) == source['sha256']
        meta = json.loads((args.input / f'{symbol}.meta.json').read_text(encoding='utf-8'))
        feed = 'aggregate' if symbol.startswith('BINANCE.') else 'trade'
        assert settings_for(symbol, feed) == meta['settings']
        assert meta['settings']['minQty'] == 0
        events = np.load(source['events_file'])['events']
        floor = meta['settings']['minNotional']
        layers, expected_prints = aggregate_events(events, start, end, floor)
        earlier = events[events['ms'] < start]
        candles, prints, raw_signature = raw_candles(symbol, meta, manifest, output, earlier['last_price'][-1])
        assert int(prints.sum()) == expected_prints, (symbol, int(prints.sum()), expected_prints)
        # Exact closes agree with the independently recorded last print of each event.
        ends = np.arange(start+TF_MS, end+1, TF_MS)
        indices = np.searchsorted(events['ms'], ends-1, side='right') - 1
        assert np.array_equal(candles[:, 3], events['last_price'][indices])
        baseline = next(r for r in old['event_summary'] if r['view'] == symbol and r['scale'] == 1)
        assert int(layers[0]['count'].sum()) == baseline['events']
        assert np.isclose(layers[0]['notional'].sum(), baseline['notional_usdt'], rtol=1e-12)
        all_layers[symbol], all_candles[symbol], all_prints[symbol], floors[symbol] = layers, candles, prints, floor
        metadata.append(dict(symbol=symbol, settings=meta['settings'], event_sha256=source['sha256'],
            raw_source_signature=raw_signature, raw_prints=expected_prints,
            exact_ohlc_close_match=True, exclusive_exhaustive_p99_partition=True))
        log(f'checked both layers and raw 30s candles: {symbol}')
    physical = list(all_layers)
    for coin in ('BTC', 'ENA', 'ZEC'):
        name, a, b = f'AGG.{coin}', f'BINANCE.{coin}USDT.P', f'OKX.{coin}-USDT-SWAP'
        all_layers[name] = [{k:all_layers[a][tier][k]+all_layers[b][tier][k]
                            for k in ('count', 'qty', 'notional')} for tier in (0, 1)]
        all_candles[name], all_prints[name] = all_candles[a], all_prints[a]
        floors[name] = min(floors[a], floors[b])
    previews, checks = {}, []
    summaries = []
    for name, layers in all_layers.items():
        previews[name], cases = preview(name, layers, all_candles[name], all_prints[name], floors[name], start)
        checks.extend(cases)
        summaries.append(summary(name, layers))
    physical_rows = [r for r in summaries if r['view'] in physical]
    totals = {k:sum(r[k] for r in physical_rows) for k in physical_rows[0] if k != 'view'}
    assert totals['large_events'] == 93538 and totals['relative_events'] == 26953
    assert totals['large_markers'] == 43823
    validation = validate_renderer(checks, output)
    result = dict(period_beijing=manifest['period_beijing'], generated_at=datetime.now(BJ).isoformat(),
        tf_seconds=30, relative_min_floor_fraction=0, totals=totals, summary=summaries,
        metadata=metadata, renderer_validation=validation,
        normalization='Both layers use unchanged original-large-trade unit from trailing 800 completed bars at each replay step.',
        candle_source='Exact raw prints; aggregate views use Binance price candles and per-venue independently classified event quantities.',
        selections='Last complete UTC day: median and maximum relative-layer bar-side circle count among 48 half-hour windows.',
        live_parameters_changed=False)
    (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    pd.DataFrame(summaries).to_csv(output / 'summary.csv', index=False, encoding='utf-8-sig')
    data = dict(period=manifest['period_beijing'], views=previews)
    (output / 'preview-data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    template = Path(__file__).with_suffix('.template.html').read_text(encoding='utf-8')
    document = template.replace('__PREVIEW_DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/'))
    document = document.replace('__LIVE_RENDERER__', (ROOT / 'static/large-trades.js').read_text(encoding='utf-8'))
    assert len(document.encode('utf-8')) < 1_000_000
    args.visual_output.parent.mkdir(parents=True, exist_ok=True)
    args.visual_output.write_text(document, encoding='utf-8')
    log(f'VISUAL {args.visual_output}; {len(document.encode("utf-8")):,} bytes')
    log(json.dumps(totals))


if __name__ == '__main__':
    main()
