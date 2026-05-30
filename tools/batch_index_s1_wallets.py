"""Mass-index S1 wallets' tx history into wallet_txs, then compute S1 TWA per wallet.

Reads S1 wallet set from server/data.json, paginates Helius enhanced API per
wallet, and STOPS EARLY once we cross the S1 lookback boundary (S1_START - 7d).
After each wallet is indexed, immediately runs compute_s1_twa() and stores the
result so the dashboard column fills in incrementally.

Resumable: a wallet that already has txs covering pre-S1 is skipped. A wallet
that has partial txs resumes paginating from its current oldest sig.

Usage:
  python tools/batch_index_s1_wallets.py                  # full S1 sweep
  python tools/batch_index_s1_wallets.py --limit 100      # bound run
  python tools/batch_index_s1_wallets.py --skip-existing  # skip wallets already in s1_twa table
"""
import os, sys, json, time, sqlite3, argparse, traceback
from datetime import datetime, UTC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from index_wallet_txs import init_db, fetch_page, get_count, get_resume_sig, DEFAULT_API_KEY  # noqa
from transform_twa import compute_s1_twa, S1_START_ISO, S1_END_ISO  # noqa
from batch_s1_twa import write_result  # noqa

DB = os.path.join(ROOT, 'data', 'solstice.db')
DATA_JSON = os.path.join(ROOT, 'server', 'data.json')

S1_START_TS = int(datetime.fromisoformat(S1_START_ISO).replace(tzinfo=UTC).timestamp())
S1_END_TS = int(datetime.fromisoformat(S1_END_ISO).replace(tzinfo=UTC).timestamp())
LOOKBACK = S1_START_TS - 7 * 86400  # pre-buffer to capture setup txs


def s1_wallets():
    d = json.load(open(DATA_JSON))
    return [r['wallet'] for r in d['records'] if r.get('is_s1')]


def wallet_covers_s1(con, wallet):
    cur = con.cursor()
    cur.execute("SELECT MIN(block_time), MAX(block_time), COUNT(*) FROM wallet_txs WHERE wallet=?", (wallet,))
    mn, mx, n = cur.fetchone()
    if not n:
        return False
    # Considered fully covered if oldest tx is at-or-before LOOKBACK, OR if
    # newest tx is after S1_END (means we've already paginated past S1 boundary).
    return (mn is not None and mn <= LOOKBACK) or (mx is not None and mx >= S1_END_TS)


def index_one(con, wallet, api_key, max_pages=200):
    """Paginate Helius for `wallet` with early-stop at LOOKBACK boundary."""
    before = get_resume_sig(con, wallet)  # paginate older than oldest known
    pages = 0
    added = 0
    t0 = time.time()
    while pages < max_pages:
        try:
            batch = fetch_page(wallet, api_key, before=before)
        except Exception as e:
            print(f"   page {pages+1} fetch failed: {e}", flush=True)
            time.sleep(3)
            continue
        if not batch:
            return added, 'no_more'
        rows = []
        for tx in batch:
            sig = tx.get('signature')
            if not sig: continue
            rows.append((
                wallet, sig, tx.get('timestamp'), tx.get('slot'),
                tx.get('feePayer'), tx.get('type'), tx.get('source'),
                1 if tx.get('transactionError') else 0,
                json.dumps(tx, separators=(',', ':')),
                int(time.time())
            ))
        cur = con.cursor()
        cur.executemany(
            "INSERT OR IGNORE INTO wallet_txs (wallet,signature,block_time,slot,fee_payer,type,source,has_error,raw_json,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows
        )
        added += cur.rowcount
        con.commit()
        pages += 1
        oldest_in_batch = batch[-1].get('signature')
        oldest_ts = batch[-1].get('timestamp', 0) or 0
        before = oldest_in_batch
        if oldest_ts and oldest_ts < LOOKBACK:
            return added, 'crossed_s1_start'
        if len(batch) < 100:
            return added, 'wallet_end'
    return added, 'max_pages'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api-key', default=DEFAULT_API_KEY)
    ap.add_argument('--limit', type=int, default=None, help='Cap wallets processed')
    ap.add_argument('--skip-existing', action='store_true',
                    help="Skip wallets already in s1_twa table")
    ap.add_argument('--max-pages', type=int, default=200,
                    help='Helius pages per wallet (100 txs/page)')
    args = ap.parse_args()

    con = init_db()
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS s1_twa (wallet TEXT PRIMARY KEY)")  # ensure exists

    wallets = s1_wallets()
    print(f"S1 wallets: {len(wallets)}", flush=True)

    if args.skip_existing:
        cur.execute("SELECT wallet FROM s1_twa")
        done = {r[0] for r in cur.fetchall()}
        wallets = [w for w in wallets if w not in done]
        print(f"--skip-existing: filtered to {len(wallets)} wallets not yet computed", flush=True)

    if args.limit:
        wallets = wallets[:args.limit]

    t_run = time.time()
    for i, w in enumerate(wallets, 1):
        t0 = time.time()
        try:
            if wallet_covers_s1(con, w):
                # Already covers S1 — just compute & write TWA.
                added, reason = 0, 'already_covered'
            else:
                added, reason = index_one(con, w, args.api_key, max_pages=args.max_pages)

            r = compute_s1_twa(w, verbose=False)
            write_result(con, r)
            dt = time.time() - t0
            elapsed = (time.time() - t_run) / 60
            print(f"[{i}/{len(wallets)}] {w[:14]}…  +{added} txs ({reason})  "
                  f"twa=${r['twa_usd']:>10,.2f}  n_w={r['n_active_days']:>3}  "
                  f"{dt:>5.1f}s  ({elapsed:.1f}min elapsed)", flush=True)
        except Exception as e:
            traceback.print_exc()
            print(f"[{i}/{len(wallets)}] {w[:14]}…  FAIL: {e}", flush=True)
            time.sleep(1)

    print(f"\nDone. total elapsed: {(time.time() - t_run)/60:.1f}min", flush=True)


if __name__ == '__main__':
    main()
