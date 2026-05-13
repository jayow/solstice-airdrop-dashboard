"""Bulk-build wallet activity (per-wallet S2 transaction history) for every
wallet with non-zero flares. Stores into DB.quest_cache key WALLET_ACTIVITY.

Run with:
  python3 scripts/build_wallet_activity.py [--limit N] [--workers 16]

Idempotent: skips wallets whose cache is fresh (<24h old).
"""
import os, sys, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
import db
import wallet_activity

p = argparse.ArgumentParser()
p.add_argument('--limit', type=int, default=0, help='only run for top-N wallets by total flares (0 = all)')
p.add_argument('--workers', type=int, default=16)
p.add_argument('--max-age-h', type=int, default=24, help='skip wallets whose activity cache is fresher than this')
p.add_argument('--wallet', type=str, default=None, help='single wallet to refresh (overrides limit)')
args = p.parse_args()

db.init()
con = db.conn()

if args.wallet:
    wallets = [args.wallet]
else:
    q = """SELECT wallet, SUM(flares) AS tot FROM wallet_quests
           GROUP BY wallet HAVING tot > 0 ORDER BY tot DESC"""
    if args.limit: q += f' LIMIT {args.limit}'
    wallets = [r['wallet'] for r in con.execute(q)]

# Skip ones with recent cache
now = int(time.time())
fresh_cutoff = now - args.max_age_h * 3600
already_fresh = set()
for r in con.execute("SELECT wallet, extracted_at FROM quest_cache WHERE quest_key='WALLET_ACTIVITY'"):
    if (r['extracted_at'] or 0) >= fresh_cutoff: already_fresh.add(r['wallet'])
todo = [w for w in wallets if w not in already_fresh]
print(f'{len(wallets):,} earning wallets · {len(already_fresh):,} already fresh · {len(todo):,} to refresh', flush=True)

t0 = time.time()
done = 0
errors = 0

def go(w):
    try:
        events = wallet_activity.cache_wallet(w, now_ts=now)
        return w, len(events), None
    except Exception as e:
        return w, 0, str(e)[:80]

with ThreadPoolExecutor(max_workers=args.workers) as ex:
    futs = [ex.submit(go, w) for w in todo]
    for fut in as_completed(futs):
        w, n, err = fut.result()
        done += 1
        if err: errors += 1
        if done % 50 == 0 or done == len(todo):
            elapsed = time.time() - t0
            eta = (len(todo) - done) * (elapsed / done) if done else 0
            print(f'  {done}/{len(todo)}  ({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining, errors={errors})', flush=True)

print(f'\nDone. {done} wallets processed in {time.time()-t0:.0f}s', flush=True)
