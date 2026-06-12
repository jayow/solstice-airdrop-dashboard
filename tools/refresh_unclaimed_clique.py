"""Re-fetch Clique allocations for every wallet whose claim_at is currently NULL/empty.

sweep_clique_allocations.py skips wallets that already have a non-_NONE_ batch,
so stale-but-batched unclaimed rows never get refreshed. This targeted script
ONLY hits the unclaimed-but-batched cohort and updates claim_at when Clique
now reports one.

Idempotent + safe: only UPDATEs the (wallet, batch) pair when Clique returns
a non-null claimAt that differs from what's stored.
"""
import os, sys, time, json, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

URL = "https://acs-v4.clique.tech/allocations"
HDR = {
    "Content-Type": "application/json",
    "Origin":       "https://claim.solstice.finance",
    "User-Agent":   "Mozilla/5.0 (compatible; HanyonAnalytics/1.0)",
}
APP_ID     = "85da4f0c"
DEPLOYMENT = "019e588b-67b6-742f-af98-104cbb6a425c"

WORKERS = 6


def fetch_one(wallet: str, sess: requests.Session) -> dict | None:
    body = {"appId": APP_ID, "deployment": DEPLOYMENT, "address": wallet}
    backoff = 2.0
    for _ in range(6):
        try:
            r = sess.post(URL, json=body, headers=HDR, timeout=15)
        except Exception:
            time.sleep(backoff); backoff = min(60, backoff * 2); continue
        if r.status_code == 429:
            time.sleep(backoff); backoff = min(60, backoff * 2); continue
        if r.status_code != 200:
            return {'_error': f'http {r.status_code}'}
        try:
            return r.json()
        except Exception:
            return None
    return {'_error': 'retries-exhausted'}


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")

    # Find wallets currently marked unclaimed (batch is real, claim_at is empty).
    rows = con.execute(
        "SELECT DISTINCT wallet FROM slx_allocations "
        "WHERE batch != '_NONE_' AND (claim_at IS NULL OR claim_at = '')"
    ).fetchall()
    wallets = [r['wallet'] for r in rows]
    con.close()
    print(f"Unclaimed-but-batched wallets to re-fetch: {len(wallets):,}", flush=True)

    sess = requests.Session()
    t0 = time.time()
    counts = {'updated': 0, 'still_unclaimed': 0, 'missing': 0, 'error': 0}

    def work(w):
        return w, fetch_one(w, sess)

    updates = []   # list of (claim_at_iso, wallet, batch)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, w) for w in wallets]
        done = 0
        for fut in as_completed(futs):
            w, resp = fut.result()
            done += 1
            if resp is None:
                counts['error'] += 1
            elif resp.get('_error'):
                counts['error'] += 1
            else:
                data = resp.get('data') or []
                changed_for_wallet = False
                for item in data:
                    batch = item.get('batchId') or item.get('batch') or ''
                    claim_at = item.get('claimAt')   # ISO or None
                    if not batch or not claim_at:
                        continue
                    updates.append((claim_at, w, batch))
                    changed_for_wallet = True
                if changed_for_wallet:
                    counts['updated'] += 1
                elif not data:
                    counts['missing'] += 1
                else:
                    counts['still_unclaimed'] += 1
            if done % 100 == 0 or done == len(wallets):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(wallets) - done) / rate if rate > 0 else 0
                print(f"  {done:>5,}/{len(wallets):,} ({elapsed:.0f}s, {rate:.1f}/s, ETA {eta:.0f}s)  {counts}", flush=True)

    print(f"\nApplying {len(updates):,} updates to slx_allocations.claim_at…", flush=True)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    n_applied = 0
    for claim_at, wallet, batch in updates:
        cur = con.execute(
            "UPDATE slx_allocations SET claim_at = ?, fetched_at = ? "
            "WHERE wallet = ? AND batch = ? AND (claim_at IS NULL OR claim_at = '')",
            (claim_at, int(time.time()), wallet, batch))
        n_applied += cur.rowcount
    con.commit()

    # Re-count unclaimed
    new_unclaimed = con.execute(
        "SELECT COUNT(DISTINCT wallet) FROM slx_allocations "
        "WHERE batch != '_NONE_' AND (claim_at IS NULL OR claim_at = '')"
    ).fetchone()[0]
    con.close()
    print(f"  applied {n_applied:,} UPDATEs to claim_at", flush=True)
    print(f"  unclaimed wallets remaining: {new_unclaimed:,}  (was {len(wallets):,})", flush=True)
    print(f"  delta: -{len(wallets) - new_unclaimed:,} wallets confirmed claimed via Clique")


if __name__ == '__main__':
    main()
