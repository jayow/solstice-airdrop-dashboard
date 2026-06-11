"""S2_HOLD_EUSX_DAILY (2×) — ground-truth walker.

Source-of-truth: every SPL token account on the EUSX mint.
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from snapshot_ts import last_snapshot_ts

from gt_walkers._base import (S2_START_TS, S2_END_TS, EUSX_MINT,
    write_walker_outputs, sync_to_wallet_quests, report, live_eusx_peg)
from gt_walkers._shared_hold import (build_twab_timeline, integrate_daily,
    integrate_matured_daily,
    discover_universe_for_mint, get_mint_supply, is_hold_cache_stale)
from quests.eusx_peg import peg_at, record_snapshot
import db

WALKER_NAME = 'gt_hold_eusx_daily'
QUEST = 'S2_HOLD_EUSX_DAILY'
MULT = 2


def run(workers: int = 16, force_refresh: bool = False) -> dict:
    with report(WALKER_NAME, QUEST, [EUSX_MINT]):
        # Ensure we have a fresh peg snapshot in the DB before integrating.
        # Then pass the time-varying peg_at(ts) callable into the integrators —
        # a constant peg over-credits early balances (eUSX compounds smoothly).
        record_snapshot()
        usd_per = peg_at
        owners = discover_universe_for_mint(EUSX_MINT)
        print(f'    {len(owners):,} unique EUSX owners', flush=True)
        now_ts = last_snapshot_ts()   # midnight-UTC cutoff
        end_ts = min(now_ts, S2_END_TS)
        results = {}

        def _flares_from_raw(raw):
            f = integrate_daily(raw.get('timeline') or [], MULT, usd_per, end_ts)
            f += integrate_matured_daily(raw.get('escrow_segs') or [], MULT, usd_per,
                                          end_ts, mature_days=7)
            return f

        def process(w):
            if not force_refresh:
                cached = db.get_cache(w, 'S2_HOLD_EUSX')
                if cached and (now_ts - (cached.get('extracted_at') or 0)) < 24*3600 \
                   and not is_hold_cache_stale(cached, w, QUEST):
                    return w, _flares_from_raw(cached['raw'])
            raw = build_twab_timeline(w, EUSX_MINT)
            if not raw.get('fetch_failed'):
                db.put_cache(w, 'S2_HOLD_EUSX', raw, watermark_ts=raw.get('last_event_ts', 0))
            return w, _flares_from_raw(raw)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(process, w) for w in owners]
            done = 0
            for fut in as_completed(futs):
                w, f = fut.result()
                done += 1
                if done % 1000 == 0: print(f'    {done}/{len(owners)}', flush=True)
                if f > 0: results[w] = f

        our_total = sum(results.values())
        upper = get_mint_supply(EUSX_MINT) * peg_at(end_ts) * MULT * (end_ts - S2_START_TS) / 86400.0
        ratio = (our_total / upper * 100) if upper > 0 else 0
        print(f'    {len(results):,} earning wallets  our_total={our_total:,.0f}  capture={ratio:.2f}% of upper', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results


if __name__ == '__main__':
    run()
