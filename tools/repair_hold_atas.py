"""One-shot repair for HOLD walker silent-empty bug.

Finds wallets where the cached HOLD quest entry has `atas: []` (no ATAs
found during a refresh) BUT the rpc_cache_responses table has a populated
getTokenAccountsByOwner response for the same wallet+mint pair — i.e. the
ATAs were known at some point and got silently wiped by an RPC failure
during a later refresh (cf. reference_walker_rpc_retry_fix).

Re-extracts each affected wallet using the patched build_twab_timeline
(which now seeds canon + prior_atas), writes the corrected cache, then
triggers a wallet_quests resync.

Usage:
  python3 tools/repair_hold_atas.py              # dry-run summary
  python3 tools/repair_hold_atas.py --apply      # actually re-extract + write
  python3 tools/repair_hold_atas.py --apply --workers 8
"""
import argparse, os, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))

from gt_walkers._shared_hold import build_twab_timeline, _MINT_CACHE_KEY
from gt_walkers._base import USX_MINT, EUSX_MINT
import db


def find_affected(conn: sqlite3.Connection, mint: str, quest_key: str) -> list[str]:
    """Wallets whose quest_cache has atas=[] but rpc_cache_responses shows a
    populated getTokenAccountsByOwner response for this mint."""
    # Build temp index on params_json's wallet+mint extraction (one-time per run).
    conn.execute('DROP TABLE IF EXISTS _rpc_idx')
    conn.execute('''
        CREATE TEMP TABLE _rpc_idx AS
        SELECT json_extract(params_json, '$[0]') AS wallet, length(response_json) AS resp_len
        FROM rpc_cache_responses
        WHERE method = 'getTokenAccountsByOwner'
          AND json_extract(params_json, '$[1].mint') = ?
          AND length(response_json) > 200
    ''', (mint,))
    conn.execute('CREATE INDEX _rpc_idx_w ON _rpc_idx(wallet)')
    rows = conn.execute('''
        SELECT q.wallet
        FROM quest_cache q
        JOIN _rpc_idx r ON r.wallet = q.wallet
        WHERE q.quest_key = ? AND q.raw_json LIKE '%"atas":[]%'
    ''', (quest_key,)).fetchall()
    return [r[0] for r in rows]


def repair_one(wallet: str, mint: str, quest_key: str) -> tuple[str, str]:
    """Re-extract and update cache. Returns (wallet, status)."""
    try:
        raw = build_twab_timeline(wallet, mint)
        atas = raw.get('atas') or []
        if not atas:
            return wallet, 'still_empty'
        nz = any(b > 0 for _, b in (raw.get('timeline') or []))
        if not nz:
            return wallet, 'no_balance'
        db.put_cache(wallet, quest_key, raw,
                     watermark_ts=raw.get('last_event_ts', 0))
        return wallet, 'repaired'
    except Exception as e:
        return wallet, f'err:{e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='actually re-extract and write (default: dry-run summary)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--limit', type=int, default=None,
                    help='cap number of wallets per mint (for incremental rollout)')
    args = ap.parse_args()

    con = sqlite3.connect(os.path.join(ROOT, 'data', 'solstice.db'))
    print('Finding USX-affected wallets…', flush=True)
    usx_w = find_affected(con, USX_MINT, 'S2_HOLD_USX')
    print(f'  {len(usx_w):,} affected USX wallets')
    print('Finding eUSX-affected wallets…', flush=True)
    eusx_w = find_affected(con, EUSX_MINT, 'S2_HOLD_EUSX')
    print(f'  {len(eusx_w):,} affected eUSX wallets')
    con.close()

    if args.limit:
        usx_w = usx_w[:args.limit]
        eusx_w = eusx_w[:args.limit]
        print(f'(limited to {args.limit} per mint)')

    if not args.apply:
        print('\nDry-run only. Re-run with --apply to repair.')
        return

    db.init()
    for label, wallets, mint, quest_key in [
        ('USX',  usx_w,  USX_MINT,  'S2_HOLD_USX'),
        ('eUSX', eusx_w, EUSX_MINT, 'S2_HOLD_EUSX'),
    ]:
        if not wallets:
            continue
        print(f'\n=== Repairing {label}: {len(wallets):,} wallets, {args.workers} workers ===')
        t0 = time.time()
        stats = {'repaired': 0, 'still_empty': 0, 'no_balance': 0}
        errs = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(repair_one, w, mint, quest_key) for w in wallets]
            for i, fut in enumerate(as_completed(futs), 1):
                w, status = fut.result()
                if status.startswith('err:'):
                    errs += 1
                else:
                    stats[status] = stats.get(status, 0) + 1
                if i % 50 == 0 or i == len(wallets):
                    rate = i / max(1, time.time() - t0)
                    print(f'  {i:>5}/{len(wallets):,}  '
                          f'repaired={stats["repaired"]}  '
                          f'still_empty={stats["still_empty"]}  '
                          f'no_balance={stats["no_balance"]}  '
                          f'err={errs}  rate={rate:.1f}/s',
                          flush=True)
        print(f'  done in {time.time()-t0:.0f}s  '
              f'repaired={stats["repaired"]}/{len(wallets)}')


if __name__ == '__main__':
    main()
