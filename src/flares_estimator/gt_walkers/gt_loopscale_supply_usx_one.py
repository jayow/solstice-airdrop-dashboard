"""S2_LOOPSCALE_SUPPLY_USX_ONE — ground-truth walker.

Source-of-truth: VaultStake accounts on Loopscale program (disc e1228035a7efb66b,
size 158). Filter at offset 8 = USX-ONE vault. Each VaultStake account contains
the real user wallet at offset 73.

The Loopscale public API keys its flares output by VaultStake PDA, NOT by user
wallet. This walker enumerates VaultStake accounts on-chain to build a PDA→user
map, then re-keys the API flares to actual user wallets. PDAs not in our map
(e.g. the vault PDA itself, internal program accounts) are dropped — they are
not real users.

Confidence: HIGH (on-chain enumeration + API math, PDA→user re-keyed).

LAYOUT (from Anchor IDL, verified 2026-05-12):
  off 0  : discriminator e1228035a7efb66b
  off 8  : vault pubkey (32) — USX-ONE vault
  off 40 : nonce/stake-id (32)
  off 72 : bump (1)
  off 73 : user pubkey (32)  ← real user wallet
"""
import os, sys, base64, base58, json
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from rpc_helper import rpc
from gt_walkers._base import write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_loopscale_supply_usx_one'
QUEST       = 'S2_LOOPSCALE_SUPPLY_USX_ONE'

LOOPSCALE_PROG  = '1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78'
VAULTSTAKE_DISC = 'e1228035a7efb66b'
USX_ONE_VAULT   = '3s3vAaYpwkyjrgzpBRwgSDxpwHPD1jic25mb1VDzM8Rk'


def _pda_to_user_map() -> dict:
    filters = [
        {'memcmp': {'offset': 0, 'bytes': base58.b58encode(bytes.fromhex(VAULTSTAKE_DISC)).decode()}},
        {'memcmp': {'offset': 8, 'bytes': USX_ONE_VAULT}},
    ]
    r = rpc('getProgramAccounts', [LOOPSCALE_PROG, {'encoding': 'base64', 'filters': filters}], timeout=120)
    out = {}
    for a in r.get('result') or []:
        d = base64.b64decode(a['account']['data'][0])
        if len(d) < 105: continue
        out[a['pubkey']] = base58.b58encode(d[73:105]).decode()
    return out


def run() -> dict:
    with report(WALKER_NAME, QUEST, [LOOPSCALE_PROG]):
        api_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_loopscale_flares.json')
        api_by_pda = {}
        if os.path.exists(api_path):
            for w, pq in json.load(open(api_path)).items():
                f = float(pq.get(QUEST, 0.0))
                if f > 0: api_by_pda[w] = f

        pda_to_user = _pda_to_user_map()

        results = {}
        dropped_flares = 0.0
        dropped_count = 0
        for pda, f in api_by_pda.items():
            user = pda_to_user.get(pda)
            if user is None:
                dropped_flares += f
                dropped_count += 1
                continue
            results[user] = results.get(user, 0.0) + f

        print(f'    API PDAs: {len(api_by_pda):,}  total={sum(api_by_pda.values()):,.0f}', flush=True)
        print(f'    VaultStake PDAs on-chain: {len(pda_to_user):,}', flush=True)
        print(f'    re-keyed to users: {len(results):,}  total={sum(results.values()):,.0f}', flush=True)
        print(f'    dropped {dropped_count} non-user PDAs ({dropped_flares:,.0f} flares — internal program accts)', flush=True)

        write_walker_outputs(WALKER_NAME, QUEST, results)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return results


if __name__ == '__main__':
    run()
