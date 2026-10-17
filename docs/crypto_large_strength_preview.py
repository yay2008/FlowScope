"""Replay display-only strength for ZEC and ENA; preserve P99 events and all totals.

Run: .venv/Scripts/python.exe docs/crypto_large_strength_preview.py --visual-output PATH
The display rank uses only prior, completed bars, separately for each coin/side.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docs.crypto_large_tier_preview import (  # noqa: E402
    BJ, TF_MS, aggregate_events, digest, log, raw_candles, summary,
)
from docs.crypto_large_floor_compare import size_unit, radius, validate_renderer  # noqa: E402
from large_trades import settings_for  # noqa: E402

HISTORY_BARS = 120
MIN_SAMPLES = 20
HIGHLIGHT_RANK = 0.8
COINS = ('ZEC', 'ENA')


def strength_ranks(qty, history_bars=HISTORY_BARS, min_samples=MIN_SAMPLES):
    """Empirical mid-rank in positive quantities from [i-120, i), never current/future.

    The two columns are sell/buy and are never pooled. Zeros do not enter the
    reference distribution. NaN means no mark or too few reference samples.
    Existing 8-decimal display precision prevents floating-point tie artefacts.
    """
    qty = np.asarray(qty).round(8)
    ranks = np.full(qty.shape, np.nan)
    counts = np.zeros(qty.shape, dtype='int16')
    for i, current in enumerate(qty):
        for side in (0, 1):
            history = qty[max(0, i-history_bars):i, side]
            history = history[history > 0]
            counts[i, side] = len(history)
            if current[side] > 0 and len(history) >= min_samples:
                ranks[i, side] = (
                    (history < current[side]).sum() + 0.5 * (history == current[side]).sum()
                ) / len(history)
    return ranks, counts


def layers_digest(layers):
    sha = hashlib.sha256()
    for layer in layers:
        for field in ('count', 'qty', 'notional'):
            sha.update(layer[field].tobytes())
    return sha.hexdigest()


def strength_summary(coin, layers, ranks, first=0, last=None):
    sliced = [{k: v[first:last] for k, v in layer.items()} for layer in layers]
    result = summary(f'AGG.{coin}', sliced)
    large = sliced[0]
    r = ranks[first:last]
    present = large['count'] > 0
    strong = present & (r >= HIGHLIGHT_RANK)
    unrated = present & np.isnan(r)
    highlighted = strong | unrated  # Cold start retains the old emphasis.
    result.update(
        highlighted_markers=int(highlighted.sum()),
        ranked_strong_markers=int(strong.sum()),
        cold_start_markers=int(unrated.sum()),
        faded_markers=int((present & ~highlighted).sum()),
        highlight_reduction=1-float(highlighted.sum()) / max(1, int(present.sum())),
        highlighted_large_events=int(large['count'][highlighted].sum()),
        highlighted_large_qty=float(large['qty'][highlighted].sum()),
        highlighted_large_qty_share=float(large['qty'][highlighted].sum()) / max(1e-30, large['qty'].sum()),
        large_qty=float(large['qty'].sum()),
        relative_qty=float(sliced[1]['qty'].sum()),
        buy_large_qty=float(large['qty'][:, 1].sum()),
        sell_large_qty=float(large['qty'][:, 0].sum()),
        large_net_qty=float((large['qty'][:, 1]-large['qty'][:, 0]).sum()),
        before_after_totals_identical=True,
    )
    return result


def selected_window(coin, layers, candles, prints, floor, ranks, counts, start, begin, stop):
    rows, units, checks = [], [], []
    for i in range(begin, stop):
        q = layers[0]['qty'][max(0, i+1-800):i+1].round(8)
        unit = float(size_unit(q, floor / candles[i, 3]))
        units.append(unit)
        if i in (begin, stop-1):
            checks.append(dict(view=f'AGG.{coin}', min_floor=floor, reference=float(candles[i, 3]),
                qty=q.tolist(), unit=unit, radii=radius(q, unit).tolist()))
        row = [float(v) for v in candles[i]]
        for field in ('qty', 'count', 'notional'):
            for layer in layers:
                row.extend(int(v) if field == 'count' else round(float(v), 8 if field == 'qty' else 4)
                           for v in layer[field][i])
        rows.append(row)
    values = ranks[begin:stop]
    assert np.isfinite(values[layers[0]['count'][begin:stop] > 0]).all(), 'Preview must be fully warmed up'
    return dict(
        start_ms=start+begin*TF_MS, rows=rows, unit=units,
        ranks=[[None if np.isnan(v) else float(v) for v in row] for row in values],
        history_samples=counts[begin:stop].tolist(),
        raw_prints=prints[begin:stop].tolist(),
        summary=strength_summary(coin, layers, ranks, begin, stop),
    ), checks


def validate_causality(qty, ranks, counts):
    split = len(qty)-240
    altered = qty.copy()
    altered[split:] = np.maximum(altered[split:], 1) * 1000
    new_ranks, new_counts = strength_ranks(altered)
    assert np.array_equal(ranks[:split], new_ranks[:split], equal_nan=True)
    assert np.array_equal(counts[:split+1], new_counts[:split+1])
    # Even changing the current bar cannot alter that bar's reference count.
    for stop in (120, 240, split):
        r, n = strength_ranks(qty[:stop])
        assert np.array_equal(r, ranks[:stop], equal_nan=True)
        assert np.array_equal(n, counts[:stop])
    one_side = qty.copy()
    one_side[:, 1] = 0
    r, _ = strength_ranks(one_side)
    assert np.array_equal(r[:, 0], ranks[:, 0], equal_nan=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'data/research/crypto-large-replay-2026-10-17')
    parser.add_argument('--visual-output', type=Path, required=True)
    args = parser.parse_args()
    output = args.input / 'strength-preview'
    output.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    manifest = json.loads((args.input / 'manifest.json').read_text(encoding='utf-8'))
    old = json.loads((args.input / 'floor-comparison/results.json').read_text(encoding='utf-8'))
    original_tiers = json.loads((args.input / 'two-tier-preview/results.json').read_text(encoding='utf-8'))
    live_hashes = {
        'large_trades.py': manifest['detector_sha256'],
        'large_trade_defaults.py': manifest['defaults_sha256'],
        'static/large-trades.js': old['renderer_sha256'],
    }
    for filename, expected in live_hashes.items():
        assert digest(ROOT / filename) == expected, f'Live source changed: {filename}'
    start, end = manifest['start_ms'], manifest['end_ms']
    all_layers, all_candles, all_prints, floors, metadata = {}, {}, {}, {}, []
    symbols = {s for coin in COINS for s in (f'BINANCE.{coin}USDT.P', f'OKX.{coin}-USDT-SWAP')}
    for source in old['provenance']:
        symbol = source['symbol']
        if symbol not in symbols:
            continue
        assert digest(source['events_file']) == source['sha256']
        meta = json.loads((args.input / f'{symbol}.meta.json').read_text(encoding='utf-8'))
        feed = 'aggregate' if symbol.startswith('BINANCE.') else 'trade'
        assert settings_for(symbol, feed) == meta['settings'] and meta['settings']['minQty'] == 0
        with np.load(source['events_file']) as archive:
            events = archive['events']
        floor = meta['settings']['minNotional']
        layers, expected_prints = aggregate_events(events, start, end, floor)
        earlier = events[events['ms'] < start]
        candles, prints, signature = raw_candles(symbol, meta, manifest, args.input / 'two-tier-preview', earlier['last_price'][-1])
        assert int(prints.sum()) == expected_prints
        ends = np.arange(start+TF_MS, end+1, TF_MS)
        indices = np.searchsorted(events['ms'], ends-1, side='right')-1
        assert np.array_equal(candles[:, 3], events['last_price'][indices])
        reference = next(v for v in original_tiers['summary'] if v['view'] == symbol)
        assert summary(symbol, layers) == reference
        all_layers[symbol], all_candles[symbol], all_prints[symbol], floors[symbol] = layers, candles, prints, floor
        metadata.append(dict(symbol=symbol, settings=meta['settings'], event_sha256=source['sha256'],
            raw_source_signature=signature, raw_prints=expected_prints, exact_ohlc_close_match=True))
        del events
        log(f'unchanged P99 / floor / both event layers: {symbol}')
    assert set(all_layers) == symbols

    ranks_by_coin, samples_by_coin, stats, full_data = {}, {}, [], {}
    for coin in COINS:
        a, b = f'BINANCE.{coin}USDT.P', f'OKX.{coin}-USDT-SWAP'
        layers = [{k: all_layers[a][tier][k]+all_layers[b][tier][k]
                   for k in ('count', 'qty', 'notional')} for tier in (0, 1)]
        before = layers_digest(layers)
        ranks, counts = strength_ranks(layers[0]['qty'])
        validate_causality(layers[0]['qty'], ranks, counts)
        assert layers_digest(layers) == before
        all_layers[coin], all_candles[coin], all_prints[coin] = layers, all_candles[a], all_prints[a]
        floors[coin], ranks_by_coin[coin], samples_by_coin[coin] = min(floors[a], floors[b]), ranks, counts
        stats.append(dict(**strength_summary(coin, layers, ranks), totals_digest=before))
        full_data[f'{coin}_ohlc'] = all_candles[coin]
        full_data[f'{coin}_rank'] = ranks
        full_data[f'{coin}_history_samples'] = counts
        for tier, name in enumerate(('large', 'relative')):
            for key, values in layers[tier].items():
                full_data[f'{coin}_{name}_{key}'] = values
    np.savez_compressed(output / 'replay-bars.npz', start_ms=start, tf_ms=TF_MS, **full_data)

    n = (end-start)//TF_MS
    day_begin = n-2880
    blocks = []
    for begin in range(day_begin, n, 60):
        row = dict(start_ms=start+begin*TF_MS, begin=begin, stop=begin+60)
        row['coins'] = {coin: strength_summary(coin, all_layers[coin], ranks_by_coin[coin], begin, begin+60) for coin in COINS}
        row['combined_large_markers'] = sum(v['large_markers'] for v in row['coins'].values())
        blocks.append(row)
    selected_begin = (int(datetime(2026, 10, 15, 13, tzinfo=BJ).timestamp()*1000)-start)//TF_MS
    dense = max((v for v in blocks if v['begin'] != selected_begin), key=lambda v: (v['combined_large_markers'], -v['begin']))
    selections = [('matched', '前次同一时段', selected_begin), ('dense', '共同密集时段', dense['begin'])]
    windows, renderer_checks = [], []
    for key, label, begin in selections:
        stamp = start+begin*TF_MS
        window = dict(key=key, label=label, start_ms=stamp,
            date=datetime.fromtimestamp(stamp/1000, BJ).strftime('%m-%d %H:%M'), coins={})
        for coin in COINS:
            window['coins'][coin], checks = selected_window(coin, all_layers[coin], all_candles[coin], all_prints[coin],
                floors[coin], ranks_by_coin[coin], samples_by_coin[coin], start, begin, begin+60)
            renderer_checks.extend(checks)
        assert window['coins']['ZEC']['start_ms'] == window['coins']['ENA']['start_ms']
        windows.append(window)
    renderer_validation = validate_renderer(renderer_checks, output)
    rule = dict(history_bars=HISTORY_BARS, history_minutes=60, min_positive_samples=MIN_SAMPLES,
        highlight_rank=HIGHLIGHT_RANK, separate_sides=True, only_positive_original_large_bars=True,
        excludes_current_and_future_bars=True, tied_values='empirical mid-rank',
        cold_start='retain original emphasis; all selected preview bars have sufficient history',
        radius='unchanged production radius; original-large quantity unit from trailing 800 completed bars',
        relative_anomaly='unchanged hollow circles; excluded from strength reference and size unit')
    data = dict(period=manifest['period_beijing'], rule=rule, windows=windows)
    result = dict(period_beijing=manifest['period_beijing'], generated_at=datetime.now(BJ).isoformat(), rule=rule,
        source=metadata, full_period=stats, windows=[dict(key=w['key'], date=w['date'],
            coins={coin: w['coins'][coin]['summary'] for coin in COINS}) for w in windows],
        last_day_half_hours=blocks,
        selections='Both coins always share the same timestamps. Default: 2026-10-15 13:00 Beijing. Stress: maximum combined original-large circle count among 48 final-UTC-day half hours, ties choose earliest.',
        validation=dict(totals_unchanged=True, no_future_data=True, independent_directions=True,
            same_timestamps=True, fully_warmed_preview=True, renderer=renderer_validation),
        live_source_hashes=live_hashes, live_parameters_changed=False)
    (output / 'preview-data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    template = Path(__file__).with_suffix('.template.html').read_text(encoding='utf-8')
    document = template.replace('__PREVIEW_DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('</', '<\\/'))
    document = document.replace('__LIVE_RENDERER__', (ROOT / 'static/large-trades.js').read_text(encoding='utf-8'))
    assert len(document.encode('utf-8')) < 1_000_000
    args.visual_output.parent.mkdir(parents=True, exist_ok=True)
    args.visual_output.write_text(document, encoding='utf-8')
    for filename, expected in live_hashes.items():
        assert digest(ROOT / filename) == expected
    log(json.dumps(result['windows'], ensure_ascii=False))
    log(f'VISUAL {args.visual_output}; {len(document.encode("utf-8")):,} bytes')


if __name__ == '__main__':
    main()
