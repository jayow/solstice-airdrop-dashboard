"""S2_REFERRAL_BONUS — intentional 0 (SIWS-gated, not on-chain).

Source-of-truth: NONE on-chain. Solstice uses SIWS-signed referral attestations
that aren't published. We document this and produce 0 flares.
"""
from gt_walkers._base import write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_referral_bonus'
QUEST = 'S2_REFERRAL_BONUS'

def run() -> dict:
    with report(WALKER_NAME, QUEST, ['siws_off_chain']):
        results = {}
        print('    intentionally empty: referral data is SIWS-gated off-chain', flush=True)
        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results

if __name__ == '__main__': run()
