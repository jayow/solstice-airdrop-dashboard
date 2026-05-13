"""Re-classify wallets currently marked as 'pda_or_uninit'.

The original classifier in filter_pdas_db.py marks a wallet as 'pda_or_uninit'
when its account doesn't exist on-chain. But many real users have no SOL
balance (just token ATAs), so their wallet address has no system account —
indistinguishable from an uninitialized PDA by that check alone.

This pass disambiguates by checking signature history: PDAs CAN'T sign txs,
so any wallet that has signed/fee-paid for a tx is a real user.

For each `pda_or_uninit` wallet:
  1. getSignaturesForAddress(limit=10)
  2. Fetch one of the txs; check if our wallet appears as fee payer (first
     account in accountKeys with signer=true)
  3. If yes → reclassify as 'real_user' (was real all along, just empty)
  4. If no  → leave as 'pda_or_uninit' (likely a true uninit PDA)

Cheap: ~2 RPC calls per candidate. With 16 workers and ~500 candidates,
runs in 1–2 min.
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db as fdb


SYSTEM_PROGRAM = '11111111111111111111111111111111'


def is_real_user(wallet: str) -> bool | None:
    """Return True if wallet is a real user, False if likely PDA, None on RPC failure.

    Two-step check:
      1. Account state: if account exists AND owner=system_program, it's a
         real user wallet (was real all along; original classifier marked it
         pda_or_uninit before the account was initialized).
      2. Tx history: if signed ≥1 tx (PDAs can't sign), it's a real user even
         if currently uninit.
    """
    try:
        # 1. Current ownership check. Retry on empty (concurrent RPC misses
        # silently return {} → v=None → would mis-classify real users as PDAs).
        v = None
        for _ in range(3):
            r = rpc('getAccountInfo', [wallet, {'encoding': 'base64'}], timeout=10, force_refresh=True)
            res = r.get('result') if isinstance(r, dict) else None
            if res is not None:
                v = res.get('value')
                break
            time.sleep(0.3)
        if v is not None and v.get('owner') == SYSTEM_PROGRAM:
            return True
        # If owner is something else (a program), it's not a user wallet — PDA-ish.
        # Note: token accounts are owned by SPL Token; those won't be queried
        # here because the universe is wallet pubkeys, not ATAs.
        if v is not None:
            return False
        # 2. Account uninit — check tx-signer history
        r = rpc('getSignaturesForAddress', [wallet, {'limit': 10}], timeout=15)
        sigs = (r.get('result') or [])
        if not sigs: return False
        for s in sigs[:3]:
            sig = s.get('signature')
            if not sig: continue
            tr = rpc('getTransaction', [sig, {'encoding': 'jsonParsed',
                                              'maxSupportedTransactionVersion': 0}], timeout=20)
            tx = (tr or {}).get('result')
            if not tx: continue
            msg = (tx.get('transaction') or {}).get('message') or {}
            keys = msg.get('accountKeys') or []
            for k in keys:
                pk = k.get('pubkey') if isinstance(k, dict) else k
                signer = bool(k.get('signer')) if isinstance(k, dict) else False
                if pk == wallet and signer:
                    return True
        return False
    except Exception:
        return None


def main():
    fdb.init()
    con = fdb.conn()
    rows = con.execute(
        "SELECT wallet FROM wallets WHERE classification='pda_or_uninit'"
    ).fetchall()
    candidates = [r['wallet'] for r in rows]
    print(f'Re-checking {len(candidates):,} pda_or_uninit wallets...', flush=True)

    n_to_real = 0; n_keep = 0; n_fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(is_real_user, w): w for w in candidates}
        for fut in as_completed(futs):
            w = futs[fut]
            r = fut.result()
            if r is True:
                con.execute("UPDATE wallets SET classification='real_user' WHERE wallet=?", (w,))
                n_to_real += 1
            elif r is None:
                n_fail += 1
            else:
                n_keep += 1
            n_done = n_to_real + n_keep + n_fail
            if n_done % 100 == 0:
                print(f'  {n_done}/{len(candidates)}  ({time.time()-t0:.0f}s)  reclassified→real={n_to_real}  kept_uninit={n_keep}  fail={n_fail}', flush=True)
    con.commit()
    print(f'\nDone in {time.time()-t0:.0f}s.')
    print(f'  → real_user: {n_to_real}')
    print(f'  → kept as pda_or_uninit: {n_keep}')
    print(f'  → RPC failures: {n_fail}')

    # How much flares does the reclassification recover?
    recovered = con.execute(
        "SELECT SUM(wq.flares) FROM wallet_quests wq JOIN wallets w ON wq.wallet=w.wallet "
        "WHERE w.classification='real_user' AND w.wallet IN (SELECT wallet FROM wallets WHERE classification='real_user') "
        "AND wq.flares > 0"
    ).fetchone()[0] or 0
    print(f'  Total real_user flares in DB now: {recovered:,.0f}')


if __name__ == '__main__':
    main()
