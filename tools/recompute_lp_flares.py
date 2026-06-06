"""Recompute LP flares for all wallets from cached S2_EXPONENT_LP events
using the patched algorithm:
  - lp_balance × sqrt(lp_price) for USD-per-second valuation
  - base mult only on Sep26 markets (no launch boost on LP — empirically
    refuted on 5V9V calibration 2026-06-06)

Avoids re-walking all signatures (cached events are already correct; only
the integration step changed). Writes to walker_outputs + wallet_quests
via the same path as the live walker.

Usage: python3 tools/recompute_lp_flares.py
"""
import json, math, os, sqlite3, sys, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))

import walker_db
from snapshot_ts import last_snapshot_ts

DB = os.path.join(ROOT, 'data', 'solstice.db')
S2_START_TS = 1776038400
S2_END_TS   = 1785024000

# Patched market config — matches walk_s2_lp.py MARKETS dict. Sep26 has no
# boost (per 5V9V calibration: launch boost was YT-only).
MARKETS = {
    'USX-Jun26':  {'mult': 20, 'quest': 'S2_EXPONENT_LP_USX_JUN26'},
    'eUSX-Jun26': {'mult': 10, 'quest': 'S2_EXPONENT_LP_EUSX_JUN26'},
    'USX-Sep26':  {'mult': 20, 'quest': 'S2_EXPONENT_LP_USX_SEP26'},
    'eUSX-Sep26': {'mult': 10, 'quest': 'S2_EXPONENT_LP_EUSX_SEP26'},
}
WALKER_QUESTS_DB = [m['quest'] for m in MARKETS.values()]


def integrate_one(evs_for_market, mult, end_ts):
    """SQRT-rate integration. Pre-S2 events seed carry-in + carry-price."""
    pre = [e for e in evs_for_market if e['ts'] < S2_START_TS]
    s2_evs = [e for e in evs_for_market if e['ts'] >= S2_START_TS]
    carry_in = max(0.0, sum(e.get('lp_delta', 0) for e in pre))
    last_price = None
    for e in reversed(pre):
        if e.get('rate'):
            last_price = math.sqrt(e['rate']); break
    lp_balance = carry_in
    usd_days = 0.0
    prev_t = S2_START_TS
    for e in s2_evs:
        t1 = e['ts']
        if t1 > prev_t and lp_balance > 0 and last_price:
            usd_days += lp_balance * last_price * (t1 - prev_t) / 86400.0
        lp_balance += e.get('lp_delta', 0)
        if e.get('rate'):
            last_price = math.sqrt(e['rate'])
        prev_t = t1
    if lp_balance > 0 and last_price and prev_t < end_ts:
        usd_days += lp_balance * last_price * (end_ts - prev_t) / 86400.0
    return usd_days * mult


def main():
    end_ts = min(last_snapshot_ts(), S2_END_TS)
    print(f'end_ts = {end_ts} (snapshot boundary)')

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = list(con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_EXPONENT_LP'"))
    print(f'Recomputing LP flares for {len(rows):,} wallets…', flush=True)

    out = defaultdict(lambda: defaultdict(float))
    n_earned = defaultdict(int)
    n_skipped = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        try:
            raw = json.loads(r['raw_json'])
        except Exception:
            n_skipped += 1; continue
        events = raw.get('events') or []
        if not events:
            continue
        by_market = defaultdict(list)
        for e in events:
            if e.get('market_label') in MARKETS:
                by_market[e['market_label']].append(e)
        for m, evs in by_market.items():
            evs.sort(key=lambda e: e['ts'])
            cfg = MARKETS[m]
            f = integrate_one(evs, cfg['mult'], end_ts)
            if f > 0:
                out[r['wallet']][cfg['quest']] = f
                n_earned[cfg['quest']] += 1
        if (i + 1) % 5000 == 0:
            print(f'  {i+1:,}/{len(rows):,} ({time.time()-t0:.0f}s)', flush=True)

    print(f'Recompute done in {time.time()-t0:.1f}s')
    print(f'Per-quest earner counts:')
    for q, n in n_earned.items():
        print(f'  {q:<32} {n:>6} wallets')

    # Persist via walker_db (same path as live walker). Prune first so
    # stale rows from the previous integration don't linger.
    print(f'\nWriting walker_outputs + wallet_quests…')
    walker_db.prune('walk_s2_lp')
    rows_db = []
    for wallet, quests in out.items():
        for quest, flares in quests.items():
            rows_db.append((wallet, quest, flares))
    walker_db.upsert_many('walk_s2_lp', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_lp', WALKER_QUESTS_DB)
    print(f'DB updated: {len(rows_db):,} walker_outputs rows, synced to wallet_quests')


if __name__ == '__main__':
    main()
