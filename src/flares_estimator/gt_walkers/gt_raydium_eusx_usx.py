"""S2_RAYDIUM_EUSX_USX — ground-truth walker.

Source-of-truth: pool program accounts (Orca whirlpool / Raydium CLMM).
"""
import os, sys, json
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from gt_walkers._base import write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_raydium_eusx_usx'
QUEST = 'S2_RAYDIUM_EUSX_USX'

def run() -> dict:
    with report(WALKER_NAME, QUEST, ['ray_eusx_usx']):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_raydium_flares.json')
        results = {}
        if os.path.exists(path):
            for w, pq in json.load(open(path)).items():
                f = float(pq.get(QUEST, 0.0))
                if f > 0: results[w] = f
        print(f'    {len(results):,} earning  total={sum(results.values()):,.0f}', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results

if __name__ == '__main__': run()
