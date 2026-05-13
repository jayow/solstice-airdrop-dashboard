"""Probe the shared recipient 5DH2e3cJ... that BOTH multisigs funded.

Goal: understand what this address is — who funds it, who it sends to,
what programs it uses, and whether it's connected to any Solstice wallet
in our dataset.

Output: data/shared_recipient_probe.json
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

TARGETS = [
    '5DH2e3cJmFpyi6mk65EGFediunm4ui6BiKNUNrhWtD1b',  # shared recipient
    '5qWya6UjwWnGVhdSBL3hyZ7B45jbk6Byt1hwd7ohEGXE',  # primary signer
]

# Known programs we want to recognize
PROGRAMS = {
    '11111111111111111111111111111111': 'System',
    'SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf': 'Squads V4',
    'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95': 'Loopscale',
    'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA': 'Token',
    'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL': 'AToken',
    'ComputeBudget111111111111111111111111111111': 'ComputeBudget',
    'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK': 'SOLSTICE presale',
    'DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB': 'SOLSTICE fee',
}

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


def all_sigs(addr, cap=5000):
    out = []
    before = None
    while len(out) < cap:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        batch = rpc('getSignaturesForAddress', params)
        if not batch: break
        out.extend(batch)
        before = batch[-1]['signature']
        if len(batch) < 1000: break
    return out


def fetch_tx(sig):
    return rpc('getTransaction', [sig, {
        'encoding': 'jsonParsed',
        'maxSupportedTransactionVersion': 0,
        'commitment': 'confirmed',
    }])


def analyze(addr):
    print(f'\n=== {addr} ===', flush=True)

    # Balance
    bal = rpc('getBalance', [addr])
    bal_sol = (bal.get('value', 0) if bal else 0) / 1e9
    print(f'  balance: {bal_sol:,.2f} SOL', flush=True)

    # Account info (owner)
    info = rpc('getAccountInfo', [addr, {'encoding': 'jsonParsed'}])
    owner = None
    if info and info.get('value'):
        owner = info['value'].get('owner')
    print(f'  owner: {owner}', flush=True)

    sigs = all_sigs(addr)
    print(f'  total sigs: {len(sigs):,}', flush=True)

    # Fetch all txs in parallel
    out = []
    def work(s):
        tx = fetch_tx(s['signature'])
        return s, tx

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(work, s) for s in sigs]
        for i, f in enumerate(as_completed(futs)):
            if i % 50 == 0: print(f'    fetched {i}/{len(sigs)}', flush=True)
            s, tx = f.result()
            if tx: out.append((s, tx))

    inflows = defaultdict(lambda: {'total': 0.0, 'count': 0, 'first_ts': 0, 'last_ts': 0, 'first_sig': None})
    outflows = defaultdict(lambda: {'total': 0.0, 'count': 0, 'first_ts': 0, 'last_ts': 0, 'first_sig': None})
    signers = Counter()
    programs_invoked = Counter()
    daily = Counter()

    for s, tx in out:
        meta = tx.get('meta') or {}
        if meta.get('err'): continue
        ts = s.get('blockTime') or 0
        sig = s['signature']
        day = dt.datetime.utcfromtimestamp(ts).isoformat()[:10] if ts else '?'
        daily[day] += 1

        msg = tx['transaction']['message']
        keys = [(k.get('pubkey') if isinstance(k, dict) else k) for k in msg.get('accountKeys', [])]
        if addr not in keys: continue
        idx = keys.index(addr)
        pre = meta.get('preBalances') or []
        post = meta.get('postBalances') or []
        if idx >= len(pre) or idx >= len(post): continue
        delta = (post[idx] - pre[idx]) / 1e9
        if idx == 0: delta += (meta.get('fee') or 0) / 1e9

        # Find the counterparty (biggest opposite-sign delta)
        best = None; max_mag = 0
        for i2, k in enumerate(keys):
            if k == addr: continue
            if i2 >= len(pre) or i2 >= len(post): continue
            d = (post[i2] - pre[i2]) / 1e9
            if i2 == 0: d += (meta.get('fee') or 0) / 1e9
            if delta > 0 and d < -0.0001 and abs(d) > max_mag:
                max_mag = abs(d); best = k
            elif delta < 0 and d > 0.0001 and abs(d) > max_mag:
                max_mag = abs(d); best = k

        # Signers = accountKeys where isSigner=true (first few)
        for k in msg.get('accountKeys', [])[:5]:
            if isinstance(k, dict) and k.get('signer'):
                signers[k['pubkey']] += 1

        # Programs invoked (from top-level + inner instructions)
        for ix in msg.get('instructions', []):
            pid = ix.get('programId') or ix.get('program')
            if pid: programs_invoked[pid] += 1
        for inner in (meta.get('innerInstructions') or []):
            for ix in inner.get('instructions', []):
                pid = ix.get('programId') or ix.get('program')
                if pid: programs_invoked[pid] += 1

        if delta > 0.0001 and best:
            r = inflows[best]
            r['total'] += delta; r['count'] += 1
            if not r['first_ts'] or ts < r['first_ts']:
                r['first_ts'] = ts; r['first_sig'] = sig
            r['last_ts'] = max(r['last_ts'], ts)
        elif delta < -0.0001 and best:
            r = outflows[best]
            r['total'] += abs(delta); r['count'] += 1
            if not r['first_ts'] or ts < r['first_ts']:
                r['first_ts'] = ts; r['first_sig'] = sig
            r['last_ts'] = max(r['last_ts'], ts)

    return {
        'balance_sol': bal_sol,
        'owner': owner,
        'sigs': len(sigs),
        'daily_tx_count': dict(sorted(daily.items())),
        'signers_top20': signers.most_common(20),
        'programs_invoked': programs_invoked.most_common(),
        'inflows': [
            {'from': k, **{kk: (vv if kk != 'total' else round(vv, 4)) for kk, vv in v.items()}}
            for k, v in sorted(inflows.items(), key=lambda x: -x[1]['total'])[:30]
        ],
        'outflows': [
            {'to': k, **{kk: (vv if kk != 'total' else round(vv, 4)) for kk, vv in v.items()}}
            for k, v in sorted(outflows.items(), key=lambda x: -x[1]['total'])[:30]
        ],
    }


def main():
    result = {}
    for addr in TARGETS:
        result[addr] = analyze(addr)

    out_path = os.path.join(ROOT, 'data/shared_recipient_probe.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    print(f'\nwrote {out_path}')

    # Pretty summary
    print('\n' + '=' * 100)
    for addr, d in result.items():
        print(f'\n{addr}')
        print(f'  balance = {d["balance_sol"]:,.2f} SOL   owner = {d["owner"]}')
        print(f'  {d["sigs"]} txs across {len(d["daily_tx_count"])} days')
        print(f'  programs:')
        for pid, n in d['programs_invoked'][:10]:
            name = PROGRAMS.get(pid, pid[:16]+'..')
            print(f'    {n:>5}x  {name}  ({pid})')
        print(f'  top signers:')
        for s, n in d['signers_top20'][:8]:
            print(f'    {n:>5}x  {s}')
        print(f'  top inflows:')
        for r in d['inflows'][:8]:
            first = dt.datetime.utcfromtimestamp(r['first_ts']).isoformat()[:10] if r['first_ts'] else '?'
            print(f'    +{r["total"]:>10.3f} SOL ({r["count"]}x)  from {r["from"]}  first={first}')
        print(f'  top outflows:')
        for r in d['outflows'][:8]:
            first = dt.datetime.utcfromtimestamp(r['first_ts']).isoformat()[:10] if r['first_ts'] else '?'
            print(f'    -{r["total"]:>10.3f} SOL ({r["count"]}x)  to   {r["to"]}  first={first}')


if __name__ == '__main__':
    main()
