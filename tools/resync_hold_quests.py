"""Resync HOLD wallet_quests from cache timelines.

For every wallet whose S2_HOLD_USX or S2_HOLD_EUSX cache has any positive
balance point, recompute integrate_daily + integrate_qualified_bonus across
all 3 tiers (DAILY / 1MO / 3MO) and upsert all 6 wallet_quests rows.

Why this exists: the bonus walkers (gt_hold_*_1mo, _3mo) only write
wallet_quests rows for wallets they CURRENTLY compute as earning. If a
wallet's cache gets repaired AFTER the bonus walker ran (e.g. via the
is_hold_cache_stale guard kicking in on the next refresh), the bonus rows
stay at their stale 0 value. This resync pass closes that gap.

Idempotent. Safe to run any number of times. Fast (~10s for ~500 wallets).
"""
import os, sys, sqlite3, json, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))

from gt_walkers._shared_hold import integrate_daily, integrate_qualified_bonus

DB = os.path.join(ROOT, 'data', 'solstice.db')
S2_END_TS = 1785024000
EUSX_PEG = 1.0319

TASKS = [
    # (cache_key, peg, [(quest_code, mult, qualify_days), ...])
    ('S2_HOLD_USX',  1.0, [
        ('S2_HOLD_USX_DAILY', 10, 0),
        ('S2_HOLD_USX_1MO',    6, 30),
        ('S2_HOLD_USX_3MO',   15, 90),
    ]),
    ('S2_HOLD_EUSX', EUSX_PEG, [
        ('S2_HOLD_EUSX_DAILY', 2, 0),
        ('S2_HOLD_EUSX_1MO',   4, 30),
        ('S2_HOLD_EUSX_3MO',  10, 90),
    ]),
]


def main():
    end_ts = min(int(time.time()), S2_END_TS)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    total_wallets = total_rows = 0
    for cache_key, peg, quests in TASKS:
        n_synced = 0
        rows = list(con.execute(f"SELECT wallet, raw_json FROM quest_cache WHERE quest_key='{cache_key}'"))
        print(f'{cache_key}: scanning {len(rows)} cache rows…', flush=True)
        c2 = sqlite3.connect(DB)
        for r in rows:
            try: raw = json.loads(r['raw_json'])
            except: continue
            tl = raw.get('timeline') or []
            max_bal = max((float(b) for _, b in tl), default=0.0) if tl else 0.0
            if max_bal == 0: continue
            # Ensure wallet exists in wallets metadata (prevents ghost rows)
            c2.execute("INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')",
                       (r['wallet'],))
            for q, m, qd in quests:
                v = (integrate_daily(tl, m, peg, end_ts) if qd == 0
                     else integrate_qualified_bonus(tl, 100.0, qd, m, peg, end_ts))
                c2.execute(
                    'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (r['wallet'], q, v, 'hold_resync', int(time.time())))
                total_rows += 1
            n_synced += 1
        c2.commit(); c2.close()
        print(f'  resynced {n_synced} wallets', flush=True)
        total_wallets += n_synced

    print(f'\nDone. {total_wallets} wallets, {total_rows} wallet_quests rows written.', flush=True)


if __name__ == '__main__':
    main()
