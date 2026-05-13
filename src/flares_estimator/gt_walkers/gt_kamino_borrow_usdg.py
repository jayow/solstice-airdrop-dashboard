"""S2_KAMINO_BORROW_USDG — ground-truth walker.

Source-of-truth: Kamino Lend program filtered by Solstice market, on-chain
enumeration of every obligation. (walk_s2_kamino.py is the actual extractor;
this walker just pulls its output and labels it per-quest in walker_outputs.)
"""
import os, sys, json, time
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from gt_walkers._base import SOLSTICE_KAMINO_MARKET, write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_kamino_borrow_usdg'
QUEST = 'S2_KAMINO_BORROW_USDG'

def run() -> dict:
    with report(WALKER_NAME, QUEST, [SOLSTICE_KAMINO_MARKET]):
        kpath = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_kamino_flares.json')
        results = {}
        if os.path.exists(kpath):
            for w, pq in json.load(open(kpath)).items():
                f = float(pq.get(QUEST, 0.0))
                if f > 0: results[w] = f
        print(f'    {len(results):,} earning  total={sum(results.values()):,.0f}', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results

if __name__ == '__main__': run()
