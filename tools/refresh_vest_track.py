"""Refresh per-wallet vest track (3mo / 9mo / unknown) for every claimed S1 wallet.

How the on-chain selection encoding works (reverse-engineered 2026-06-14):

  Clique distributes S1 SLX through 3 MerkleDistributor PDAs under program
  85F1bj5k85LZxzHM35epKtHD5E11HcYsxLpV8VbyT6od:
    Fs5AQwKF…  U1 immediate-unlock distributor (claimed at TGE May 25)
    FQsNXCX…   3-month vest distributor (opt-in)
    GuTrjtwi…  9-month vest distributor (default)

  Each distributor has one or more ClaimRoot PDAs holding 32-byte Merkle roots.
  Clique's /allocations endpoint returns the wallet's batch list with proofs.

  Merkle leaf format (verified against a known U1 claim):
    handler = keccak256(wallet_pubkey_32)
    leaf    = keccak256(handler || allocation_LE_u64)
    pair    = keccak256(min(a,b) || max(a,b))    # OpenZeppelin sorted-pair

  Classification logic (empirical, 200-wallet sample):
    1 unclaimed vest batch  + root ∈ {3mo roots}  →  vest_track = '3mo'
    2 unclaimed vest batches + both ∈ {9mo roots} →  vest_track = '9mo_default'
    1 unclaimed vest batch  + root ∈ {9mo roots}  →  vest_track = '9mo_selected' (none seen)
    0 unclaimed vest batches                       →  vest_track = 'u1_only'
    proof matches no known root                    →  vest_track = 'unknown' (anomaly)

  Run nightly alongside refresh_unclaimed_clique.py — vest selections lock in
  at U2 (2026-07-05) so post-U2 the column becomes the on-chain truth.
"""
import os, sqlite3, time, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import base58, requests
from Crypto.Hash import keccak as _keccak_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

URL = "https://acs-v4.clique.tech/allocations"
APP_ID     = "85da4f0c"
DEPLOYMENT = "019e588b-67b6-742f-af98-104cbb6a425c"
HDR = {
    "Content-Type": "application/json",
    "Origin":       "https://claim.solstice.finance",
    "User-Agent":   "Mozilla/5.0 (compatible; HanyonAnalytics/1.0)",
}

# Known Merkle roots (from program 85F1bj5k's ClaimRoot PDAs filtered by SLX distributor)
ROOTS_3MO = {
    '8b778cae4a69d436e19510167521fbf38321b62c163491f900f3f4963109e994',
    '4de2128ca4297bc735cbcf9d2cae3353dc549e0811608ad0fcc33fc07de659a4',
}
ROOTS_9MO = {
    'fb7ffb00f38626c24c13fb422b5cd16fe87174e4d1e1d85014dcacf5edde8662',
    'c00007642d9251325a7c1b26f1c43670a62c15e6baf60e30d782c896e4f87787',
}

def keccak256(d):
    h = _keccak_mod.new(digest_bits=256); h.update(d); return h.digest()

def pair_keccak_sorted(a, b):
    if a > b: a, b = b, a
    return keccak256(a + b)

def derive_root(wallet, allocation, proof_hex_list):
    """Reconstruct the Merkle root that this proof + allocation chain up to."""
    handler = keccak256(base58.b58decode(wallet))
    leaf = keccak256(handler + int(allocation).to_bytes(8, 'little'))
    cur = leaf
    for p in proof_hex_list:
        sib = bytes.fromhex(p[2:] if p.startswith('0x') else p)
        cur = pair_keccak_sorted(cur, sib)
    return cur.hex()

def classify_wallet(wallet, sess: requests.Session) -> tuple[str, list, str | None]:
    """Returns (vest_track, matched_roots, error_or_none)."""
    body = {"appId": APP_ID, "deployment": DEPLOYMENT, "address": wallet}
    backoff = 1.5
    for _ in range(5):
        try:
            r = sess.post(URL, json=body, headers=HDR, timeout=20)
        except Exception:
            time.sleep(backoff); backoff = min(20, backoff * 2); continue
        if r.status_code in (429, 503):
            time.sleep(backoff); backoff = min(20, backoff * 2); continue
        if r.status_code != 200:
            return ('error', [], f'http {r.status_code}')
        try:
            data = r.json().get('data', [])
            break
        except Exception:
            return ('error', [], 'json decode')
    else:
        return ('error', [], 'rate-limit exhausted')

    unclaimed = [b for b in data if not b.get('claimAt')]
    if not unclaimed:
        return ('u1_only', [], None)

    matched = []
    for b in unclaimed:
        extra = b.get('extra', {})
        proof = extra.get('proof', [])
        alloc = int(b.get('allocation', 0))
        if alloc == 0 or not proof:
            return ('error', [], 'missing proof/alloc')
        root_hex = derive_root(wallet, alloc, proof)
        if root_hex in ROOTS_3MO:   matched.append('3mo')
        elif root_hex in ROOTS_9MO: matched.append('9mo')
        else:                        matched.append(f'unknown:{root_hex[:12]}')

    if any(m.startswith('unknown') for m in matched):
        return ('unknown', matched, None)

    # 1 batch and it's 3mo → explicitly selected 3mo
    if len(matched) == 1 and matched[0] == '3mo':
        return ('3mo', matched, None)
    # 1 batch and it's 9mo → explicitly selected 9mo
    if len(matched) == 1 and matched[0] == '9mo':
        return ('9mo_selected', matched, None)
    # 2 batches both 9mo → user hasn't actively selected; default = 9mo at U2
    if len(matched) == 2 and all(m == '9mo' for m in matched):
        return ('9mo_default', matched, None)
    # Anything else (shouldn't happen given the sampling)
    return ('unknown_shape', matched, None)

def ensure_schema(con):
    cur = con.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(slx_allocations)").fetchall()}
    if 'vest_track' not in cols:
        print("  schema: adding vest_track column to slx_allocations")
        cur.execute("ALTER TABLE slx_allocations ADD COLUMN vest_track TEXT")
        cur.execute("ALTER TABLE slx_allocations ADD COLUMN vest_track_at INTEGER")
    con.commit()

def run(workers=4, only_missing=True, limit=None):
    con = sqlite3.connect(DB)
    ensure_schema(con)
    if only_missing:
        rows = con.execute("""
            SELECT wallet FROM slx_allocations
            WHERE claim_at != '' AND batch != '_NONE_'
              AND (vest_track IS NULL OR vest_track = '' OR vest_track = 'error')
            ORDER BY total_slx DESC
        """).fetchall()
    else:
        rows = con.execute("""
            SELECT wallet FROM slx_allocations
            WHERE claim_at != '' AND batch != '_NONE_'
            ORDER BY total_slx DESC
        """).fetchall()
    wallets = [r[0] for r in rows]
    if limit: wallets = wallets[:limit]
    print(f"  to classify: {len(wallets):,} claimed wallets (workers={workers})")

    sess = requests.Session()
    t0 = time.time()
    done = 0
    counts = {}
    last_print = time.time()
    BATCH = 100  # commit every 100 to keep the DB up to date
    pending_updates = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(classify_wallet, w, sess): w for w in wallets}
        for fut in as_completed(futs):
            wallet = futs[fut]
            try:
                track, matched, err = fut.result()
            except Exception as e:
                track, matched, err = 'error', [], str(e)[:60]
            counts[track] = counts.get(track, 0) + 1
            pending_updates.append((track, int(time.time()), wallet))
            done += 1

            if len(pending_updates) >= BATCH or done == len(wallets):
                cur = con.cursor()
                cur.executemany("""
                    UPDATE slx_allocations
                    SET vest_track = ?, vest_track_at = ?
                    WHERE wallet = ? AND claim_at != ''
                """, pending_updates)
                con.commit()
                pending_updates = []

            if time.time() - last_print > 10:
                last_print = time.time()
                dt = time.time() - t0
                rate = done / max(dt, 1)
                eta = (len(wallets) - done) / max(rate, 0.01)
                summary = ' '.join(f'{k}={v}' for k, v in sorted(counts.items(), key=lambda x:-x[1])[:5])
                print(f"  [{done}/{len(wallets)}] {dt:.0f}s · {rate:.1f}/s · ETA {eta/60:.1f}min · {summary}", flush=True)

    dt = time.time() - t0
    print(f"\n  done in {dt/60:.1f}min")
    print(f"  classified: {sum(counts.values()):,} wallets")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"    {k:>20}: {v:>6,}")

if __name__ == '__main__':
    import sys
    workers = 4
    only_missing = True
    limit = None
    for a in sys.argv[1:]:
        if a == '--all':       only_missing = False
        elif a.startswith('--workers='): workers = int(a.split('=')[1])
        elif a.startswith('--limit='):   limit = int(a.split('=')[1])
    run(workers=workers, only_missing=only_missing, limit=limit)
