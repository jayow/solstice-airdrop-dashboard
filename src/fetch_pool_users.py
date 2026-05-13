"""Scrape signatures for a list of Solstice-partner pools (Orca/Raydium),
then batch-fetch the transactions to extract the signer for each sig.
Writes per-pool jsonl + an aggregated users set.

Env:
  RPC_URL   default: https://solana-rpc.publicnode.com
Usage:
  python3 src/fetch_pool_users.py <pool_key>   (scrape just one)
  python3 src/fetch_pool_users.py              (scrape all)
"""
import json, os, sys, time, threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'data/partner_pools')
os.makedirs(OUT_DIR, exist_ok=True)

RPC = os.environ.get('RPC_URL', 'https://solana-rpc.publicnode.com')

POOLS = {
    'orca_usx_usdc':     '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix',
    'orca_eusx_usx':     'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf',
    'orca_usx_usdg':     '45bdcbekD687TU49RFux1a4csf3TN3cM3J1UaFcFhWt2',
    'raydium_usx_usdc':  'EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6',
    'raydium_eusx_usx':  'BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n',
}

SIG_CONCURRENCY = 1   # single-threaded sig pagination (back-walk is serial anyway)
TX_CONCURRENCY = 6
TIMEOUT = 25

session = requests.Session()
write_lock = threading.Lock()


def rpc(method, params, retries=8):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    for attempt in range(retries):
        try:
            r = session.post(RPC, json=body, timeout=TIMEOUT)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5 * (2 ** attempt)))
                continue
            r.raise_for_status()
            j = r.json()
            if 'error' in j:
                msg = str(j['error'])
                if 'rate' in msg.lower() or 'limit' in msg.lower():
                    time.sleep(min(8, 0.5 * (2 ** attempt)))
                    continue
                raise RuntimeError(msg)
            return j.get('result')
        except requests.RequestException:
            time.sleep(min(4, 0.5 * (2 ** attempt)))
    raise RuntimeError(f'rpc {method} retries exhausted')


def load_sigs(pool_key):
    path = os.path.join(OUT_DIR, f'{pool_key}.sigs.json')
    if os.path.exists(path):
        return json.load(open(path))
    return []


def save_sigs(pool_key, sigs):
    path = os.path.join(OUT_DIR, f'{pool_key}.sigs.json')
    with open(path, 'w') as f:
        json.dump(sigs, f)


def fetch_sigs(pool_key, addr, back_days=None):
    """Fetch sigs for an address. Two-phase walk:
       Phase A — forward: from newest, stop when hitting existing.
       Phase B — backward: from oldest-existing, keep going until RPC empty (pool inception).
       back_days=None means fetch all history.
    """
    cutoff_ts = (time.time() - back_days * 86400) if back_days else None
    existing = load_sigs(pool_key)
    seen = {s['signature'] for s in existing}

    newer = []
    # Phase A — forward catchup
    before = None
    page = 0
    while True:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        batch = rpc('getSignaturesForAddress', params)
        if not batch: break
        page += 1
        stop = False
        for s in batch:
            if s['signature'] in seen:
                stop = True; break
            if cutoff_ts and (s.get('blockTime') or 0) < cutoff_ts:
                stop = True; break
            newer.append(s)
        before = batch[-1]['signature']
        oldest = batch[-1].get('blockTime') or 0
        print(f'  [{pool_key}] fwd page {page}: +{len(batch)} (new {len(newer)}, oldest {time.strftime("%Y-%m-%d", time.gmtime(oldest))})', flush=True)
        if stop or len(batch) < 1000:
            break

    # Phase B — backward backfill from oldest existing
    older = []
    if existing:
        # The existing list is newest-first; oldest is last.
        before = existing[-1]['signature']
        page = 0
        while True:
            params = [addr, {'limit': 1000, 'before': before}]
            batch = rpc('getSignaturesForAddress', params)
            if not batch: break
            page += 1
            new_this_page = 0
            for s in batch:
                if s['signature'] in seen: continue
                if cutoff_ts and (s.get('blockTime') or 0) < cutoff_ts: continue
                older.append(s); new_this_page += 1
            before = batch[-1]['signature']
            oldest = batch[-1].get('blockTime') or 0
            print(f'  [{pool_key}] back page {page}: +{new_this_page} (backfill {len(older)}, oldest {time.strftime("%Y-%m-%d", time.gmtime(oldest))})', flush=True)
            if len(batch) < 1000: break

    all_sigs = newer + existing + older  # keep newest-first ordering
    save_sigs(pool_key, all_sigs)
    print(f'  [{pool_key}] sigs total: {len(all_sigs):,} (+fwd {len(newer):,}, +back {len(older):,})', flush=True)
    return all_sigs


def load_signer_map(pool_key):
    path = os.path.join(OUT_DIR, f'{pool_key}.signers.jsonl')
    signer_by_sig = {}
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
                if r.get('signer'):
                    signer_by_sig[r['sig']] = r['signer']
            except Exception:
                pass
    return signer_by_sig


def fetch_tx_signer(sig):
    """Return (sig, signer) tuple. Uses getTransaction and reads accountKeys[0]."""
    try:
        tx = rpc('getTransaction', [sig, {
            'encoding': 'jsonParsed',
            'maxSupportedTransactionVersion': 0,
            'commitment': 'confirmed',
        }])
        if not tx:
            return (sig, None, 'no_tx')
        msg = tx.get('transaction', {}).get('message', {})
        keys = msg.get('accountKeys', [])
        if not keys:
            return (sig, None, 'no_keys')
        k0 = keys[0]
        signer = k0.get('pubkey') if isinstance(k0, dict) else k0
        return (sig, signer, None)
    except Exception as e:
        return (sig, None, str(e)[:60])


def scrape_pool(pool_key, addr, back_days=180):
    print(f'\n=== {pool_key} ({addr}) ===', flush=True)
    sigs = fetch_sigs(pool_key, addr, back_days=back_days)
    known = load_signer_map(pool_key)
    todo = [s['signature'] for s in sigs if s['signature'] not in known and not s.get('err')]
    print(f'  already have signer for: {len(known):,}; need to fetch: {len(todo):,}', flush=True)

    out_path = os.path.join(OUT_DIR, f'{pool_key}.signers.jsonl')
    out = open(out_path, 'a')
    t0 = time.time()
    done = 0

    try:
        with ThreadPoolExecutor(max_workers=TX_CONCURRENCY) as ex:
            futs = {ex.submit(fetch_tx_signer, sig): sig for sig in todo}
            for fut in as_completed(futs):
                sig, signer, err = fut.result()
                row = {'sig': sig}
                if signer:
                    row['signer'] = signer
                if err:
                    row['err'] = err
                with write_lock:
                    out.write(json.dumps(row) + '\n')
                done += 1
                if done % 500 == 0 or done == len(todo):
                    dt = time.time() - t0
                    rate = done / dt if dt else 0
                    eta = (len(todo) - done) / rate if rate else 0
                    print(f'  [{pool_key}] {done:>6}/{len(todo)}  {rate:.1f}/s  eta={eta:.0f}s', flush=True)
    finally:
        out.close()

    # Count unique signers
    sm = load_signer_map(pool_key)
    signers = set(sm.values())
    print(f'  [{pool_key}] unique signers: {len(signers):,}', flush=True)
    return signers


def main():
    keys = sys.argv[1:] or list(POOLS.keys())
    for k in keys:
        if k not in POOLS:
            print(f'unknown pool key: {k}', file=sys.stderr); continue
        scrape_pool(k, POOLS[k], back_days=180)

    # Final overlap report
    fp = {x['sender'] for x in json.load(open(os.path.join(ROOT, 'data/fee_payers.json')))}
    print(f'\n=== Solstice fee-payer overlap ===')
    print(f'{"Pool":<22s} {"users":>8s} {"overlap":>8s} {"%":>6s}')
    for k in POOLS:
        sm = load_signer_map(k)
        signers = set(sm.values())
        if not signers: continue
        ov = signers & fp
        pct = 100 * len(ov) / len(fp) if fp else 0
        print(f'{k:<22s} {len(signers):>8,} {len(ov):>8,} {pct:>5.1f}%')


if __name__ == '__main__':
    main()
