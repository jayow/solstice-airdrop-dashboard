"""S2_HOLD_USX_1MO (6×) — ground-truth walker.

Source-of-truth: every SPL token account on the USX mint.
Fires when wallet holds ≥$100 continuously for ≥30d.
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))

from gt_walkers._base import (S2_START_TS, S2_END_TS, USX_MINT,
    write_walker_outputs, sync_to_wallet_quests, report, live_eusx_peg)
from gt_walkers._shared_hold import (build_twab_timeline, integrate_qualified_bonus,
    discover_universe_for_mint)
import db

WALKER_NAME = 'gt_hold_usx_1mo'
QUEST = 'S2_HOLD_USX_1MO'
MULT = 6
MIN_BAL = 100.0
QUALIFY_DAYS = 30


def run(workers: int = 16, force_refresh: bool = False) -> dict:
    with report(WALKER_NAME, QUEST, [USX_MINT]):
        usd_per = 1.0
        owners = discover_universe_for_mint(USX_MINT)
        print(f'    {len(owners):,} unique USX owners', flush=True)
        now_ts = int(time.time())
        end_ts = min(now_ts, S2_END_TS)
        results = {}

        def process(w):
            if not force_refresh:
                cached = db.get_cache(w, 'S2_HOLD_USX')
                if cached and (now_ts - (cached.get('extracted_at') or 0)) < 24*3600:
                    return w, integrate_qualified_bonus(cached['raw'].get('timeline') or [], MIN_BAL, QUALIFY_DAYS, MULT, usd_per, end_ts)
            raw = build_twab_timeline(w, USX_MINT)
            db.put_cache(w, 'S2_HOLD_USX', raw, watermark_ts=raw.get('last_event_ts', 0))
            return w, integrate_qualified_bonus(raw.get('timeline') or [], MIN_BAL, QUALIFY_DAYS, MULT, usd_per, end_ts)

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
        n_qualifying = len(results)
        print(f'    {n_qualifying:,} qualifying wallets  our_total={our_total:,.0f}  (mult={MULT}, min=${MIN_BAL:.0f}, qualify={QUALIFY_DAYS}d)', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results


if __name__ == '__main__':
    run()
