"""One-shot fix: re-walk legacy schema-1 HOLD caches and correct wallet_quests.

The daily HOLD walker only processes CURRENT holders (discover_universe_for_mint),
so a wallet that WITHDREW is never re-processed — its stale schema-1 cache (zero
timeline) and last-computed flares persist. resync_hold_quests SKIPS max_bal==0
caches, so it can't fix them either. Result: ~430 wallets carry phantom HOLD
flares (cache-schema drift; see reference_cache_schema_drift).

This re-walks each schema-1 wallet's real ATA history (cold walk seeded from the
cached ATAs, so it works even for withdrawn wallets), rewrites the cache as
schema-2, then recomputes DAILY/1MO/3MO from the corrected timeline and upserts
wallet_quests. The integrate fns return 0 for a corrected zero timeline, so true
phantoms zero out and held-then-withdrew wallets get their correct earned flares.
Mirrors resync_hold_quests' exact config (integrate_daily / integrate_qualified_bonus,
per-token peg, midnight cutoff). Idempotent.
"""
import os, sys, json, time, sqlite3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
from concurrent.futures import ThreadPoolExecutor, as_completed
import db
from gt_walkers._shared_hold import build_twab_timeline, integrate_daily, integrate_qualified_bonus
from gt_walkers._base import USX_MINT, EUSX_MINT
from quests.eusx_peg import peg_at
from snapshot_ts import last_snapshot_ts

DB = os.path.join(ROOT, 'data', 'solstice.db')
S2_END_TS = 1785024000
END_TS = min(last_snapshot_ts(), S2_END_TS)

TASKS = [
    ('S2_HOLD_USX',  USX_MINT,  1.0,    [('S2_HOLD_USX_DAILY', 10, 0), ('S2_HOLD_USX_1MO', 6, 30), ('S2_HOLD_USX_3MO', 15, 90)]),
    ('S2_HOLD_EUSX', EUSX_MINT, peg_at, [('S2_HOLD_EUSX_DAILY', 2, 0), ('S2_HOLD_EUSX_1MO', 4, 30), ('S2_HOLD_EUSX_3MO', 10, 90)]),
]


def main():
    db.init()
    for cache_key, mint, peg, quests in TASKS:
        con = sqlite3.connect(DB)
        # Only schema-1 caches that carry POSITIVE flares are the phantoms worth
        # correcting — the thousands of other schema-1 caches are 0-flare and
        # need no fix (re-walking them is wasted RPC).
        qcodes = tuple(q for q, _, _ in quests)
        ph = ','.join('?' * len(qcodes))
        flared = {r[0] for r in con.execute(
            f"SELECT DISTINCT wallet FROM wallet_quests WHERE quest IN ({ph}) AND flares > 0", qcodes)}
        s1 = []
        for w, rj in con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key=?", (cache_key,)):
            if w not in flared:
                continue
            try:
                if json.loads(rj).get('_schema') is None:
                    s1.append(w)
            except Exception:
                pass
        con.close()
        print(f'{cache_key}: {len(s1)} flare-positive schema-1 caches → re-walking (cold)', flush=True)

        def rewalk(w):
            try:
                raw = build_twab_timeline(w, mint)
                if not raw.get('fetch_failed'):
                    db.put_cache(w, cache_key, raw, watermark_ts=raw.get('last_event_ts', 0))
                    return w, raw
                return w, None    # RPC failed — leave prior cache/flares untouched
            except Exception as e:
                print(f'  ✗ {w[:10]}… {e}', flush=True)
                return w, None

        results = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(rewalk, w) for w in s1]
            for i, fut in enumerate(as_completed(futs)):
                results.append(fut.result())
                if (i + 1) % 50 == 0:
                    print(f'  re-walked {i+1}/{len(s1)}', flush=True)

        # Recompute + upsert from corrected caches (0 timeline → 0 flares).
        c2 = sqlite3.connect(DB); c2.execute('PRAGMA busy_timeout=30000')
        now = int(time.time()); zeroed = held = failed = 0
        for w, raw in results:
            if raw is None:
                failed += 1
                continue
            tl = raw.get('timeline') or []
            max_bal = max((float(b) for _, b in tl), default=0.0) if tl else 0.0
            for q, m, qd in quests:
                v = (integrate_daily(tl, m, peg, END_TS) if qd == 0
                     else integrate_qualified_bonus(tl, 100.0, qd, m, peg, END_TS))
                c2.execute('INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                           'VALUES (?, ?, ?, ?, ?)', (w, q, v, 'hold_schema1_fix', now))
            if max_bal > 0:
                held += 1
            else:
                zeroed += 1
        c2.commit(); c2.close()
        print(f'  done: {held} held (corrected), {zeroed} phantom (zeroed), {failed} RPC-failed (left as-is)', flush=True)


if __name__ == '__main__':
    main()
