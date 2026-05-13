"""S2_EXPONENT_YIELD_USX_JUN26 (30×) — ground-truth walker.

Source-of-truth: every YieldPosition account (disc e35c92, size 164) on the
Exponent program where yp_alias matches the USX-Jun26 market. Holder wallet is
at offset 8 (authority). Unioned with DB.quest_cache to include wallets whose
positions have since been closed.

Per-wallet flares come from the ExponentYTExtractor transform applied to
DB.quest_cache['S2_EXPONENT_YT']. The orchestrator force-refresh pass populates
that cache; this walker only does the active-holder enumeration + transform.

Confidence: HIGH (direct on-chain enumeration + DB-cached extraction).
"""
import os, sys, time
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from gt_walkers._base import S2_START_TS, USX_JUN26_MARKET, write_walker_outputs, sync_to_wallet_quests, report
from gt_walkers._shared_yt import market_meta, yt_holder_universe
from quests.exponent_yt import ExponentYTExtractor
import db

WALKER_NAME = 'gt_exponent_yield_usx_jun26'
QUEST = 'S2_EXPONENT_YIELD_USX_JUN26'


def run() -> dict:
    with report(WALKER_NAME, QUEST, [USX_JUN26_MARKET]):
        meta = market_meta(USX_JUN26_MARKET)
        print(f'    yt_mint={meta["yt_mint"][:10]}..  yp_alias={meta["yp_alias"][:10]}..  supply={meta["yt_supply"]:,.2f}', flush=True)
        wallets = yt_holder_universe(meta['yp_alias'])
        print(f'    {len(wallets):,} candidate wallets (active on-chain ∪ cached history)', flush=True)

        e = ExponentYTExtractor()
        now_ts = int(time.time())
        results = {}
        for w in wallets:
            cached = db.get_cache(w, 'S2_EXPONENT_YT')
            if not cached: continue
            f = e.transform(cached['raw'], now_ts).get(QUEST, 0.0)
            if f > 0: results[w] = f
        print(f'    {len(results):,} earning  total={sum(results.values()):,.0f}', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results


if __name__ == '__main__':
    run()
