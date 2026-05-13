"""DB-backed PDA filter.

For each wallet in DB.wallet_quests with non-zero flares:
  - getAccountInfo(wallet)
  - If owner == System Program (`1111…`) → real user wallet
  - If owner is anything else (a program) → PDA (protocol-owned)
  - If account doesn't exist (no SOL ever sent) → suspect PDA (mark as 'pda_or_uninit')

Marks `wallets.classification = 'pda'` for filtered addresses. The DB-backed
build_data.py then excludes them.

Run:
  python3 src/flares_estimator/filter_pdas_db.py
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db

SYSTEM_PROGRAM = '11111111111111111111111111111111'

# Known DeFi protocol programs — wallets owned by these are unambiguous PDAs
KNOWN_PROTOCOL_PROGRAMS = {
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'kamino_lend',
    'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7': 'exponent',
    'XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy': 'exponent_v2_generic',
    'XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC': 'exponent_v2_clmm',
    'eUSXyKoZ6aGejYVbnp3wtWQ1E8zuokLAJPecPxxtgG3': 'eusx_yield_vault',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'orca_whirlpool',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'raydium_clmm',
    '6LtLpnUFNByNXLyCoK9wA2MykKAmQNZKBdY8s47dehDc': 'kamino_liquidity',
    'KvauGMspG5k6rtzrqqn7WNn3oZdyKqLKwK2XWQ8FLjd': 'kamino_vault',
    'kVauTFR8qm1dhniz6pYuBZkuene3Hfrs1VQhVRgCNrr': 'kamino_kvault_v2',
    'F1ipperKF9EfD821ZbbYjS319LXYiBmjhzkkf5a26rC': 'flipper',
    'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4': 'jupiter',
    'SVMD2yJUTeYpsmCPnUF7gFnoBpDgFFvfDzbWPKDFf6h': 'svm',
    # Loopscale
    '1oopBoJG58DgkUVKkEzK': 'loopscale_partial',   # partial prefix
}


def is_pda_program(owner: str) -> bool:
    if not owner or owner == SYSTEM_PROGRAM: return False
    # Match by prefix (some loopscale variants seen)
    for prog, _ in KNOWN_PROTOCOL_PROGRAMS.items():
        if owner.startswith(prog[:18]) or prog.startswith(owner[:18]):
            return True
    # Unknown program — still treat as PDA (real wallets are System-owned)
    return True


def classify(wallet: str) -> str:
    """Returns 'user' | 'pda' | 'unknown'"""
    try:
        r = rpc('getAccountInfo', [wallet, {'encoding': 'base64'}], timeout=10)
        v = r.get('result', {}).get('value')
        if v is None:
            # Account doesn't exist on-chain — for our purposes, treat as suspect PDA
            return 'pda_or_uninit'
        owner = v.get('owner')
        if owner == SYSTEM_PROGRAM:
            return 'user'
        return 'pda'
    except Exception:
        return 'unknown'


def main():
    db.init()
    c = db.conn()
    # Find every wallet with positive flares
    wallets = [r['wallet'] for r in c.execute(
        'SELECT DISTINCT wallet FROM wallet_quests WHERE flares > 0'
    )]
    print(f'Classifying {len(wallets):,} wallets with positive flares...', flush=True)

    counts = {'user':0, 'pda':0, 'pda_or_uninit':0, 'unknown':0}
    pda_wallets = []

    def work(w):
        return w, classify(w)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(work, w) for w in wallets]
        done = 0
        for fut in as_completed(futs):
            done += 1
            w, kind = fut.result()
            counts[kind] += 1
            if kind in ('pda', 'pda_or_uninit'):
                pda_wallets.append((w, kind))
            if done % 500 == 0: print(f'  {done}/{len(wallets)}  ({time.time()-t0:.0f}s)', flush=True)

    print(f'\nClassification result ({time.time()-t0:.1f}s):')
    for k, n in counts.items(): print(f'  {k:<20s} {n:>6,}')

    # Mark PDAs in DB
    print(f'\nMarking {len(pda_wallets)} wallets as PDAs in wallets.classification...')
    with db.txn() as conn:
        for w, kind in pda_wallets:
            conn.execute(
                'INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, ?)',
                (w, kind)
            )
            conn.execute(
                'UPDATE wallets SET classification = ? WHERE wallet = ?',
                (kind, w)
            )

    # Sum of flares being filtered out
    pda_set = {w for w, _ in pda_wallets}
    pda_flares = sum(
        r['total'] for r in c.execute(
            'SELECT wallet, SUM(flares) AS total FROM wallet_quests GROUP BY wallet'
        ) if r['wallet'] in pda_set
    )
    print(f'  → flares attributable to filtered PDAs: {pda_flares:,.0f}')
    print('\nDone. Run `python3 server/build_data.py` to refresh the dashboard.')


if __name__ == '__main__':
    main()
