"""Backfill eUSX peg snapshots from on-chain transaction Program returns.

Walks all signatures touching the eUSX vault state PDA
(2rjmJdxZeBLKjUjKjrSdtSqshgXbLEcPbjiJZ2vLNcCT) inside the window
[S2_START, 2026-05-14], fetches each tx, and decodes the peg from the
"Program return: XP1BRLn8e... <base64>" lines (len=68 blobs; peg is u128 LE
at offset 0, scale 1e12). Inserts one snapshot per (blockTime, peg) into
data/solstice.db.eusx_peg_snapshots.
"""
import os, sys, base64, re, sqlite3, time, json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

# Read RPC URL from .env
RPC_URL = None
with open(os.path.join(ROOT, '.env')) as f:
    for line in f:
        line = line.strip()
        if line.startswith('HELIUS_API_KEY='):
            v = line.split('=', 1)[1].strip()
            if v.startswith('http'):
                RPC_URL = v
            else:
                RPC_URL = f'https://mainnet.helius-rpc.com/?api-key={v}'
            break
assert RPC_URL, 'HELIUS_API_KEY not found'

VAULT_PDA = '2rjmJdxZeBLKjUjKjrSdtSqshgXbLEcPbjiJZ2vLNcCT'
EUSX_PROG = 'XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy'
S2_START = 1776038400   # 2026-04-13 00:00 UTC
WINDOW_END = 1779164000 # 2026-05-14

PROG_RET_RE = re.compile(r'Program return: ' + EUSX_PROG + r' (\S+)')


def rpc(method, params, timeout=30):
    r = requests.post(RPC_URL, json={'jsonrpc': '2.0', 'id': 1,
                                     'method': method, 'params': params},
                      timeout=timeout)
    return r.json()


def gather_sigs_in_window():
    sigs = []
    before = None
    n_pages = 0
    while True:
        params = [VAULT_PDA, {'limit': 1000}]
        if before:
            params[1]['before'] = before
        r = rpc('getSignaturesForAddress', params)
        res = r.get('result') or []
        if not res:
            break
        for s in res:
            bt = s.get('blockTime') or 0
            if S2_START <= bt <= WINDOW_END:
                sigs.append((s['signature'], bt))
        n_pages += 1
        before = res[-1]['signature']
        last_bt = res[-1].get('blockTime') or 0
        # Stop once we're well before window start
        if last_bt and last_bt < S2_START - 86400:
            break
        if n_pages % 10 == 0:
            print(f'  scanned {n_pages} pages, in-window so far: {len(sigs)}, oldest bt {last_bt}', flush=True)
    return sigs


def decode_pegs_from_tx(sig_bt):
    sig, bt = sig_bt
    for attempt in range(4):
        try:
            r = rpc('getTransaction', [sig, {'maxSupportedTransactionVersion': 0,
                                             'encoding': 'jsonParsed'}], timeout=30)
            tx = r.get('result')
            if tx is None:
                return (bt, None)
            pegs = []
            for L in (tx.get('meta', {}).get('logMessages') or []):
                m = PROG_RET_RE.search(L)
                if not m:
                    continue
                try:
                    blob = base64.b64decode(m.group(1))
                except Exception:
                    continue
                if len(blob) != 68:
                    continue
                val = int.from_bytes(blob[:16], 'little')
                peg = val / 1e12
                if 1.0 < peg < 1.5:
                    pegs.append(peg)
            if not pegs:
                return (bt, None)
            # Use the maximum (latest in tx) — pegs only ever increase intra-tx,
            # and the late-tx GetState reflects post-state.
            return (bt, max(pegs))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return (bt, None)


def main():
    print(f'Gathering sigs for {VAULT_PDA} in [{S2_START}, {WINDOW_END}]...', flush=True)
    sigs = gather_sigs_in_window()
    print(f'Found {len(sigs)} sigs in window.', flush=True)

    # Decode in parallel
    snapshots = []
    decoded = 0
    with ThreadPoolExecutor(max_workers=24) as ex:
        futures = {ex.submit(decode_pegs_from_tx, sb): sb for sb in sigs}
        for i, fut in enumerate(as_completed(futures)):
            bt, peg = fut.result()
            if peg is not None:
                snapshots.append((bt, peg))
                decoded += 1
            if (i + 1) % 500 == 0:
                print(f'  decoded {decoded}/{i + 1} ({100*decoded/(i+1):.1f}%)', flush=True)

    print(f'Decoded {decoded} pegs from {len(sigs)} txs.', flush=True)
    if not snapshots:
        print('No pegs decoded. Aborting write.', flush=True)
        return

    # Write to DB
    with sqlite3.connect(DB) as c:
        c.executemany('INSERT OR REPLACE INTO eusx_peg_snapshots(ts, peg) VALUES (?, ?)',
                      snapshots)
        c.commit()
    print(f'Wrote {len(snapshots)} snapshots.', flush=True)

    # Verify
    with sqlite3.connect(DB) as c:
        count = c.execute('SELECT COUNT(*) FROM eusx_peg_snapshots WHERE ts BETWEEN ? AND ?',
                          (S2_START, WINDOW_END)).fetchone()[0]
    print(f'Snapshots in [{S2_START}, {WINDOW_END}] after backfill: {count}', flush=True)


if __name__ == '__main__':
    main()
