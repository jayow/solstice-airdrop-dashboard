"""S2_HOLD_USX_DAILY (10×) — ground-truth walker.

Source-of-truth: every SPL token account on the USX mint (`6FrrzDk5…`).
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from snapshot_ts import last_snapshot_ts

from gt_walkers._base import (
    S2_START_TS, S2_END_TS, USX_MINT,
    write_walker_outputs, sync_to_wallet_quests, report,
)
from gt_walkers._shared_hold import (
    build_twab_timeline, integrate_daily,
    discover_universe_for_mint, get_mint_supply, is_hold_cache_stale,
)
import db

WALKER_NAME = 'gt_hold_usx_daily'
QUEST = 'S2_HOLD_USX_DAILY'
MULT = 10
USD_PER = 1.0


def run(workers: int = 16, force_refresh: bool = False) -> dict:
    with report(WALKER_NAME, QUEST, [USX_MINT]):
        print(f'    discovering all USX holders…', flush=True)
        owners = discover_universe_for_mint(USX_MINT)
        print(f'    {len(owners):,} unique USX owners on-chain', flush=True)

        now_ts = last_snapshot_ts()   # midnight-UTC cutoff
        end_ts = min(now_ts, S2_END_TS)
        results = {}

        def process(w):
            if not force_refresh:
                cached = db.get_cache(w, 'S2_HOLD_USX')
                if cached and (now_ts - (cached.get('extracted_at') or 0)) < 24*3600 \
                   and not is_hold_cache_stale(cached, w, QUEST):
                    return w, integrate_daily(cached['raw'].get('timeline') or [], MULT, USD_PER, end_ts)
            raw = build_twab_timeline(w, USX_MINT)
            db.put_cache(w, 'S2_HOLD_USX', raw, watermark_ts=raw.get('last_event_ts', 0))
            return w, integrate_daily(raw.get('timeline') or [], MULT, USD_PER, end_ts)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(process, w) for w in owners]
            done = 0
            for fut in as_completed(futs):
                w, f = fut.result()
                done += 1
                if done % 1000 == 0: print(f'    {done}/{len(owners)}  ({time.time()-t0:.0f}s)', flush=True)
                if f > 0: results[w] = f

        our_total = sum(results.values())
        mint_supply = get_mint_supply(USX_MINT)
        days = (end_ts - S2_START_TS) / 86400.0
        upper = mint_supply * USD_PER * MULT * days
        print(f'    {len(results):,} earning wallets', flush=True)
        print(f'    our total: {our_total:,.0f} flares', flush=True)
        print(f'    upper-bound (full supply × {MULT}× × {days:.2f}d): {upper:,.0f}', flush=True)
        ratio = (our_total / upper * 100) if upper > 0 else 0
        print(f'    capture: {ratio:.2f}% of upper-bound (real avg-bal < total supply → <100% expected)', flush=True)

        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results


if __name__ == '__main__':
    run()
