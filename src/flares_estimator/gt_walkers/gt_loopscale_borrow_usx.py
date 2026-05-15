"""S2_LOOPSCALE_BORROW_USX — ground-truth walker.

HYBRID SOURCE:
  1. Loopscale API → authoritative per-wallet flares (covers 27 wallets, ~93M).
     The API computes from exact principal-vs-time history.
  2. On-chain enumeration → 203 active USX loans on Loopscale program
     `1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78`, disc=14c34675a5e3b601,
     filtered by principal_mint=USX at offset 92. Covers 116 wallets, ~595M
     aggregate. Per-wallet flares = principal_remaining × MULT × days_open.

ATTRIBUTION:
  For wallets in API → use API value (exact).
  For wallets only on-chain → use on-chain estimate.

LOAN STRUCT (anchored to IDL fetched 2026-05-12, disc 14c34675a5e3b601):
  off 8   : version (u8)
  off 9   : bump (u8)
  off 10  : loan_status (u8, 0=active)
  off 11  : borrower pubkey (32 bytes)  ← real user wallet
  off 43  : created_at (u64 unix-seconds)
  off 92  : principal_mint (32 bytes)
  off 155 : principal_remaining_raw (u64, units 1e9 = 1 USX)
"""
import os, sys, base64, base58, struct, time, json
from concurrent.futures import ThreadPoolExecutor, as_completed
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from snapshot_ts import last_snapshot_ts
from rpc_helper import rpc
from gt_walkers._base import S2_START_TS, write_walker_outputs, sync_to_wallet_quests, report

WALKER_NAME = 'gt_loopscale_borrow_usx'
QUEST       = 'S2_LOOPSCALE_BORROW_USX'
MULT        = 5

LOOPSCALE_PROG  = '1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78'
LOAN_DISC_HEX   = '14c34675a5e3b601'
USX_MINT        = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
PRINCIPAL_SCALE = 1e9


def _enum_onchain(now_ts: int) -> dict:
    filters = [
        {'memcmp': {'offset': 0,  'bytes': base58.b58encode(bytes.fromhex(LOAN_DISC_HEX)).decode()}},
        {'memcmp': {'offset': 92, 'bytes': USX_MINT}},
    ]
    r = rpc('getProgramAccounts', [LOOPSCALE_PROG, {'encoding': 'base64', 'filters': filters}], timeout=180)
    accs = r.get('result') or []
    out = {}
    for acc in accs:
        d = base64.b64decode(acc['account']['data'][0])
        if len(d) < 200: continue
        if d[10] != 0: continue  # only active loans
        borrower = base58.b58encode(d[11:43]).decode()
        created = struct.unpack_from('<Q', d, 43)[0]
        principal = struct.unpack_from('<Q', d, 155)[0] / PRINCIPAL_SCALE
        if created <= 0 or created > now_ts: continue
        start = max(created, S2_START_TS)
        days = max(0.0, (now_ts - start) / 86400.0)
        flares = principal * MULT * days
        if flares > 0:
            out[borrower] = out.get(borrower, 0.0) + flares
    return out


def run() -> dict:
    with report(WALKER_NAME, QUEST, [LOOPSCALE_PROG]):
        now_ts = last_snapshot_ts()   # midnight-UTC cutoff
        # API path
        api_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'data', 's2_loopscale_flares.json')
        api_results = {}
        if os.path.exists(api_path):
            for w, pq in json.load(open(api_path)).items():
                f = float(pq.get(QUEST, 0.0))
                if f > 0: api_results[w] = f
        # On-chain path
        oc_results = _enum_onchain(now_ts)

        # Merge: API authoritative; on-chain fills gaps
        merged = dict(api_results)
        added = 0
        for w, f in oc_results.items():
            if w not in merged:
                merged[w] = f
                added += 1

        print(f'    API: {len(api_results):,} wallets  total={sum(api_results.values()):,.0f}', flush=True)
        print(f'    on-chain: {len(oc_results):,} wallets  total={sum(oc_results.values()):,.0f}', flush=True)
        print(f'    merged: {len(merged):,} wallets ({added} added from on-chain)  total={sum(merged.values()):,.0f}', flush=True)

        # Persist on-chain raw for inspection
        with open(api_path.replace('s2_loopscale_flares.json', 's2_loopscale_borrow_onchain.json'), 'w') as f:
            json.dump(oc_results, f, indent=2)

        write_walker_outputs(WALKER_NAME, QUEST, merged)
        sync_to_wallet_quests(WALKER_NAME, QUEST)
    return merged


if __name__ == '__main__':
    run()
