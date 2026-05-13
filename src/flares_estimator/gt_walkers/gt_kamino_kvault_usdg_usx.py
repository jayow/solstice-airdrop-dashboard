"""S2_KAMINO_KVAULT_USDG_USX (10×) — ground-truth walker.

Source-of-truth: SPL share-mint holders of the Kamino USDG/USX Strategy
share token (4qkStdH1...). (walk_s2_kamino_strategy.py is the extractor.)
"""
import os, sys, json, time
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from gt_walkers._base import write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_kamino_kvault_usdg_usx'
QUEST = 'S2_KAMINO_KVAULT_USDG_USX'
SHARE_MINT = '4qkStdH1NPKMmxrTDbY8kzTkJorpGMd8GLxo81drv9Jz'

def run() -> dict:
    with report(WALKER_NAME, QUEST, [SHARE_MINT]):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_kamino_strategy_flares.json')
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
