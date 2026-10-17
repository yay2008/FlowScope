"""Verify production detector/renderer against the approved ZEC/ENA research replay.

Uses the archived merged events without contacting an exchange or the running service.
Run: .venv/Scripts/python.exe docs/crypto_large_strength_verify.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from large_trades import LargeTradeDetector, decorate_bars  # noqa: E402
from indicator import TZ_SHIFT_S  # noqa: E402


def main():
    folder = ROOT / 'data/research/crypto-large-replay-2026-10-17'
    reference = json.loads((folder / 'strength-preview/preview-data.json').read_text(encoding='utf-8'))
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    sources = json.loads((folder / 'floor-comparison/results.json').read_text(encoding='utf-8'))['provenance']
    assert hashlib.sha256((ROOT / 'large_trade_defaults.py').read_bytes()).hexdigest() == manifest['defaults_sha256']
    grouped = {(w['key'], coin): [] for w in reference['windows'] for coin in ('ZEC', 'ENA')}
    checked_events, original_events, relative_events, cases = 0, 0, 0, []
    for coin in ('ZEC', 'ENA'):
        for symbol, source in [(f'BINANCE.{coin}USDT.P', 'aggregate'), (f'OKX.{coin}-USDT-SWAP', 'trade')]:
            meta = json.loads((folder / f'{symbol}.meta.json').read_text(encoding='utf-8'))
            path = folder / f'{symbol}.events.npz'
            provenance = next(v for v in sources if v['symbol'] == symbol)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance['sha256']
            with np.load(path) as archive:
                events = archive['events']
            for w in reference['windows']:
                since, until = w['start_ms']-3600_000, w['start_ms']+1800_000
                # Extra 30 minutes warms up the unchanged 10-minute P99 baseline.
                rows = events[(events['ms'] >= since-1800_000) & (events['ms'] < until)]
                d = LargeTradeDetector(symbol, source, meta['settings'])
                for e in rows:
                    key = int(e['id'])
                    d._append(key, (int(e['ms'])*1_000_000, float(e['price']), float(e['qty']), int(e['side']),
                                    bool(e['reset']), True, not bool(e['reset'])))
                    if e['ms'] < since:
                        continue
                    expected_large, expected_relative = e['category'] == 2, e['category'] == 3
                    assert (key in d.events) == expected_large, (symbol, int(e['ms']), 'large')
                    assert (key in d.relative.events) == expected_relative, (symbol, int(e['ms']), 'relative')
                    if expected_large or expected_relative:
                        actual = d.events[key] if expected_large else d.relative.events[key]
                        assert np.isclose(actual['threshold'], e['p99_qty'], rtol=1e-12)
                    checked_events += 1
                    original_events += int(expected_large)
                    relative_events += int(expected_relative)
                grouped[(w['key'], coin)].append(d)
            print(f'verified detector: {symbol}', flush=True)

    for w in reference['windows']:
        for coin in ('ZEC', 'ENA'):
            detectors = grouped[(w['key'], coin)]
            t0 = w['start_ms']//1000-3600+TZ_SHIFT_S
            bars = [dict(time=t0+i*30, volume=0) for i in range(180)]
            decorate_bars(bars, detectors, 30)
            approved = w['coins'][coin]
            for bar, row in zip(bars[-60:], approved['rows']):
                for field, q0, c0 in [('large', 4, 8), ('largeRelative', 6, 10)]:
                    s = bar[field]   # 空心层没事件的根只给 count/coverage/cvd
                    assert s['count'] == row[c0]+row[c0+1]
                    assert np.isclose(s.get('sell', 0.0), row[q0], rtol=1e-12)
                    assert np.isclose(s.get('buy', 0.0), row[q0+1], rtol=1e-12)
                    assert np.isclose(s.get('delta', 0.0), row[q0+1]-row[q0], rtol=1e-12, atol=1e-8)
            cases.append(dict(coin=coin, window=w['key'], bars=bars, configs=[d.config() for d in detectors], expected=approved))

    # Execute the actual CommonJS renderer. No copied rank or opacity implementation.
    script = r'''
const fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
const large=require(path.join(process.argv[1],'static/large-trades.js'));
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
let ranks=0,markers=0;const windows=[];
for(const c of cases){
  const before=JSON.stringify(c.bars),scores=large.strengths(c.bars,c.configs,30);
  let highlighted=0,primary=0,relative=0;
  for(let i=0;i<60;i++){
    const score=scores[120+i],expected=c.expected.ranks[i],samples=c.expected.history_samples[i];
    for(const [side,col] of [['sell',0],['buy',1]]){
      assert.equal(score[side].samples,samples[col]);
      assert.equal(score[side].rank,expected[col]);ranks++;
    }
    for(const m of large.markers(c.bars[120+i],c.expected.unit[i],score)){
      assert.equal(m.r,large.radius(m.qty,c.expected.unit[i]));markers++;
      if(m.tier==='large'){primary++;if(m.style.highlighted)highlighted++;}
      else {relative++;assert.equal(m.style,null);}
    }
  }
  assert.equal(primary,c.expected.summary.large_markers);
  assert.equal(relative,c.expected.summary.relative_markers);
  assert.equal(highlighted,c.expected.summary.highlighted_markers);
  assert.equal(JSON.stringify(c.bars),before,'Display calculation changed statistics');
  windows.push({coin:c.coin,window:c.window,primary,highlighted,relative});
}
console.log(JSON.stringify({rank_checks:ranks,marker_checks:markers,windows,passed:true}));
'''
    run = subprocess.run(['node', '-e', script, str(ROOT)], input=json.dumps(cases), text=True,
                         capture_output=True, check=True, cwd=ROOT)
    result = dict(detector_events_checked=checked_events, unchanged_original_events=original_events,
                  independently_recorded_relative_events=relative_events, frontend=json.loads(run.stdout),
                  thresholds_unchanged=True, original_bar_statistics_unchanged=True,
                  live_service_modified=False)
    destination = folder / 'strength-preview/production-verification.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
