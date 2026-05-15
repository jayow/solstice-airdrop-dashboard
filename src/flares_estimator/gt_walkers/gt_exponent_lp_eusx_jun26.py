"""SS2_EXPONENT_LP_EUSX_JUN26 — ground-truth walker.

Source-of-truth: LP market vault sigs + every Exponent-LP position cache.
"""
import os, sys, time, json
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from snapshot_ts import last_snapshot_ts
from gt_walkers._base import S2_START_TS, write_walker_outputs, sync_to_wallet_quests, report
from quests.exponent_lp import ExponentLPExtractor
import db

WALKER_NAME = 'gt_exponent_lp_eusx_jun26'
QUEST = 'S2_EXPONENT_LP_EUSX_JUN26'

def run() -> dict:
    with report(WALKER_NAME, QUEST, ['LP market vault']):
        e = ExponentLPExtractor()
        now_ts = last_snapshot_ts()   # midnight-UTC cutoff
        results = {}
        # Source of truth: the walked LP outputs in data/s2_lp_flares.json
        # (which itself enumerates LP-vault sigs program-wide).
        lp_json = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_lp_flares.json')
        if os.path.exists(lp_json):
            lp = json.load(open(lp_json))
            for w, pq in lp.items():
                f = float(pq.get(QUEST, 0.0))
                if f > 0: results[w] = f
        print(f'    {len(results):,} earning  total={sum(results.values()):,.0f}', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results

if __name__ == '__main__': run()
