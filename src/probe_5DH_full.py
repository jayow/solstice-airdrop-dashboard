"""Full historical probe of 5DH2e3cJ — walk back to genesis to find creation + early funding.

We cap at 25000 sigs (should cover all history since multisigs typically aren't older than a year).
"""
import os, json, time, datetime as dt
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

ADDR = '5DH2e3cJmFpyi6mk65EGFediunm4ui6BiKNUNrhWtD1b'

session = requests.Session()


def rpc(method, params, retries=6):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    for i in range(retries):
        try:
            r = session.post(URL, json=body, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5 * (2 ** i))); continue
            j = r.json()
            if 'error' in j:
                time.sleep(0.5); continue
            return j.get('result')
        except requests.RequestException:
            time.sleep(0.5 * (i + 1))
    return None


def all_sigs(addr, cap=25000):
    out = []
    before = None
    while len(out) < cap:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        batch = rpc('getSignaturesForAddress', params)
        if not batch: break
        out.extend(batch)
        before = batch[-1]['signature']
        print(f'    fetched {len(out)} sigs (oldest: {dt.datetime.utcfromtimestamp(out[-1].get("blockTime") or 0).isoformat()[:10]})', flush=True)
        if len(batch) < 1000: break
    return out


def fetch_tx(sig):
    return rpc('getTransaction', [sig, {
        'encoding': 'jsonParsed',
        'maxSupportedTransactionVersion': 0,
        'commitment': 'confirmed',
    }])


def main():
    print(f'Probing {ADDR}')
    bal = rpc('getBalance', [ADDR])
    print(f'balance: {(bal.get("value",0) if bal else 0)/1e9:,.2f} SOL')

    # Deep sig walk
    sigs = all_sigs(ADDR)
    print(f'\ntotal sigs: {len(sigs):,}')
    if sigs:
        oldest_ts = sigs[-1].get('blockTime') or 0
        newest_ts = sigs[0].get('blockTime') or 0
        print(f'range: {dt.datetime.utcfromtimestamp(oldest_ts).isoformat()}  →  {dt.datetime.utcfromtimestamp(newest_ts).isoformat()}')

    # Examine first (oldest) tx to see what created this account
    if sigs:
        first_sig = sigs[-1]['signature']
        print(f'\n=== GENESIS TX: {first_sig} ===')
        tx = fetch_tx(first_sig)
        if tx:
            meta = tx.get('meta') or {}
            msg = tx['transaction']['message']
            keys = [(k.get('pubkey') if isinstance(k, dict) else k) for k in msg.get('accountKeys', [])]
            signers = [k.get('pubkey') for k in msg.get('accountKeys', []) if isinstance(k, dict) and k.get('signer')]
            programs = [ix.get('programId') or ix.get('program') for ix in msg.get('instructions', [])]
            pre = meta.get('preBalances') or []
            post = meta.get('postBalances') or []
            print(f'  ts: {dt.datetime.utcfromtimestamp((tx.get("blockTime") or 0)).isoformat()}')
            print(f'  signers: {signers}')
            print(f'  programs: {programs}')
            if ADDR in keys:
                idx = keys.index(ADDR)
                delta = (post[idx] - pre[idx]) / 1e9 if idx < len(pre) and idx < len(post) else 0
                print(f'  {ADDR} delta: {delta:+.4f} SOL (pre={pre[idx]/1e9 if idx<len(pre) else "?":.4f}, post={post[idx]/1e9 if idx<len(post) else "?":.4f})')
            # Who funded it?
            print(f'  all balance deltas:')
            for i, k in enumerate(keys):
                if i >= len(pre) or i >= len(post): continue
                d = (post[i] - pre[i]) / 1e9
                if abs(d) > 0.0001:
                    print(f'    {d:+9.4f} SOL  {k}')

    # Sample earliest N txs to understand what this thing does
    print(f'\n=== Fetching full tx data for complete history ===')
    print(f'This will take a while: {len(sigs):,} txs\n')

    out = []
    def work(s): return s, fetch_tx(s['signature'])

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(work, s) for s in sigs]
        for i, f in enumerate(as_completed(futs)):
            if i % 500 == 0: print(f'  {i}/{len(sigs)}', flush=True)
            s, tx = f.result()
            if tx: out.append((s, tx))

    inflows = defaultdict(lambda: {'total': 0.0, 'count': 0, 'first_ts': 0, 'first_sig': None})
    outflows = defaultdict(lambda: {'total': 0.0, 'count': 0, 'first_ts': 0, 'first_sig': None})
    signers = Counter()
    programs_invoked = Counter()
    monthly = Counter()

    for s, tx in out:
        meta = tx.get('meta') or {}
        if meta.get('err'): continue
        ts = s.get('blockTime') or 0
        sig = s['signature']
        mo = dt.datetime.utcfromtimestamp(ts).isoformat()[:7] if ts else '?'
        monthly[mo] += 1

        msg = tx['transaction']['message']
        keys = [(k.get('pubkey') if isinstance(k, dict) else k) for k in msg.get('accountKeys', [])]
        if ADDR not in keys: continue
        idx = keys.index(ADDR)
        pre = meta.get('preBalances') or []
        post = meta.get('postBalances') or []
        if idx >= len(pre) or idx >= len(post): continue
        delta = (post[idx] - pre[idx]) / 1e9
        if idx == 0: delta += (meta.get('fee') or 0) / 1e9

        best = None; max_mag = 0
        for i2, k in enumerate(keys):
            if k == ADDR: continue
            if i2 >= len(pre) or i2 >= len(post): continue
            d = (post[i2] - pre[i2]) / 1e9
            if i2 == 0: d += (meta.get('fee') or 0) / 1e9
            if delta > 0 and d < -0.0001 and abs(d) > max_mag:
                max_mag = abs(d); best = k
            elif delta < 0 and d > 0.0001 and abs(d) > max_mag:
                max_mag = abs(d); best = k

        for k in msg.get('accountKeys', [])[:5]:
            if isinstance(k, dict) and k.get('signer'):
                signers[k['pubkey']] += 1

        for ix in msg.get('instructions', []):
            pid = ix.get('programId') or ix.get('program')
            if pid: programs_invoked[pid] += 1

        if delta > 0.0001 and best:
            r = inflows[best]
            r['total'] += delta; r['count'] += 1
            if not r['first_ts'] or ts < r['first_ts']:
                r['first_ts'] = ts; r['first_sig'] = sig
        elif delta < -0.0001 and best:
            r = outflows[best]
            r['total'] += abs(delta); r['count'] += 1
            if not r['first_ts'] or ts < r['first_ts']:
                r['first_ts'] = ts; r['first_sig'] = sig

    result = {
        'total_sigs': len(sigs),
        'balance_sol': (bal.get('value', 0) if bal else 0) / 1e9,
        'monthly_tx_count': dict(sorted(monthly.items())),
        'signers_top30': signers.most_common(30),
        'programs_invoked': programs_invoked.most_common(),
        'inflows': sorted([
            {'from': k, 'total': round(v['total'], 4), 'count': v['count'],
             'first_ts': v['first_ts'], 'first_sig': v['first_sig']}
            for k, v in inflows.items()
        ], key=lambda x: -x['total']),
        'outflows': sorted([
            {'to': k, 'total': round(v['total'], 4), 'count': v['count'],
             'first_ts': v['first_ts'], 'first_sig': v['first_sig']}
            for k, v in outflows.items()
        ], key=lambda x: -x['total']),
    }

    out_path = os.path.join(ROOT, 'data/5DH_full_probe.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    print(f'\nwrote {out_path}')

    # Summary
    print(f'\n{ADDR}')
    print(f'  {result["total_sigs"]:,} txs across {len(result["monthly_tx_count"])} months')
    print(f'  monthly activity:')
    for mo, n in result['monthly_tx_count'].items():
        print(f'    {mo}: {n:,}')
    print(f'\n  top 15 signers (distinct wallets):')
    for s, n in result['signers_top30'][:15]:
        print(f'    {n:>5}x  {s}')
    print(f'\n  programs:')
    for pid, n in result['programs_invoked']:
        print(f'    {n:>5}x  {pid}')
    print(f'\n  TOP INFLOWS (total SOL received from each source):')
    for r in result['inflows'][:15]:
        first = dt.datetime.utcfromtimestamp(r['first_ts']).isoformat()[:10] if r['first_ts'] else '?'
        print(f'    +{r["total"]:>10.3f} SOL ({r["count"]:>4}x)  from {r["from"]}  first={first}  sig={r["first_sig"][:16]}..')
    print(f'\n  TOP OUTFLOWS:')
    for r in result['outflows'][:15]:
        first = dt.datetime.utcfromtimestamp(r['first_ts']).isoformat()[:10] if r['first_ts'] else '?'
        print(f'    -{r["total"]:>10.3f} SOL ({r["count"]:>4}x)  to   {r["to"]}  first={first}  sig={r["first_sig"][:16]}..')


if __name__ == '__main__':
    main()
