"""Batch compute S1 TWA TVL across all S1 wallets.

Reads the S1 wallet set from server/data.json (records with is_s1=True), skips
any wallet whose wallet_txs is not yet indexed (must be populated first by
tools/index_wallet_txs.py), and writes results to data/solstice.db:s1_twa.

Resumable: rows with a fresher computed_at than tx-indexed-at can be skipped
via --skip-existing. Process-local RPC caches in transform_twa amortize
market_info / historical_sy_per_lp lookups across wallets.

Usage:
  python tools/batch_s1_twa.py                    # compute for all S1 wallets that have indexed txs
  python tools/batch_s1_twa.py --only WALLET ...  # specific wallets
  python tools/batch_s1_twa.py --skip-existing    # skip wallets already in s1_twa table
  python tools/batch_s1_twa.py --limit 50         # bound the run
"""
import os, sys, json, time, sqlite3, argparse, traceback
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from transform_twa import compute_s1_twa  # noqa: E402

DB = os.path.join(ROOT, 'data', 'solstice.db')
DATA_JSON = os.path.join(ROOT, 'server', 'data.json')


def s1_wallets_from_data_json():
    d = json.load(open(DATA_JSON))
    return [r['wallet'] for r in d['records'] if r.get('is_s1')]


def already_indexed_wallets(con):
    cur = con.cursor()
    cur.execute("SELECT DISTINCT wallet FROM wallet_txs")
    return {r[0] for r in cur.fetchall()}


def already_computed(con):
    cur = con.cursor()
    cur.execute("SELECT wallet FROM s1_twa")
    return {r[0] for r in cur.fetchall()}


def write_result(con, r):
    con.execute("""
        INSERT OR REPLACE INTO s1_twa
        (wallet, twa_usd, twab_full_season, twab_since_first, sum_daily,
         n_active_days, N_w, first_active_day, last_active_day,
         peak_tvl, peak_ts, sources_json, tx_count, computed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r['wallet'], r['twa_usd'], r['twab_full_season'], r['twab_since_first'],
        r['sum_daily'], r['n_active_days'], r['N_w'],
        r['first_active_day'], r['last_active_day'],
        r['peak_tvl'], r['peak_ts'],
        json.dumps(r['sources']), r['tx_count'], int(time.time()),
    ))
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='+', help='Explicit wallet list (overrides S1 set)')
    ap.add_argument('--skip-existing', action='store_true', help='Skip wallets already in s1_twa')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()

    con = sqlite3.connect(DB)

    if args.only:
        wallets = list(args.only)
    else:
        s1 = s1_wallets_from_data_json()
        indexed = already_indexed_wallets(con)
        wallets = [w for w in s1 if w in indexed]
        print(f"S1 wallets: {len(s1)} | indexed: {len(indexed)} | ready: {len(wallets)}")

    if args.skip_existing:
        done = already_computed(con)
        before = len(wallets)
        wallets = [w for w in wallets if w not in done]
        print(f"--skip-existing: skipped {before - len(wallets)} already computed")

    if args.limit:
        wallets = wallets[:args.limit]

    print(f"Computing {len(wallets)} wallets...")

    ok = 0
    fail = 0
    for i, w in enumerate(wallets, 1):
        t0 = time.time()
        try:
            r = compute_s1_twa(w, verbose=False)
            write_result(con, r)
            dt = time.time() - t0
            print(f"[{i}/{len(wallets)}] {w}  twa=${r['twa_usd']:>10,.2f}  "
                  f"n_w={r['n_active_days']:>3}  txs={r['tx_count']:>5}  {dt:>5.1f}s")
            ok += 1
        except Exception as e:
            traceback.print_exc()
            print(f"[{i}/{len(wallets)}] {w}  FAIL: {e}")
            fail += 1

    print(f"\nDone. ok={ok} fail={fail}")


if __name__ == '__main__':
    main()
