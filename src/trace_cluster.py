"""BFS trace of the cohort-1-mystery funding cluster.
Starts at the identified wallets, walks upstream through SOL transfers,
tags everything against known Solstice datasets + known Solana entities.

Output: data/cluster_trace.json with the full graph + writes a readable summary.
"""
import os, json, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

SEED = [
    'FA1qnqZWMptKtmyQ4DQvufH5tiYentEyvStPehsJfCTk',       # cohort 1 mystery
    'EY7yPT1nJr7AWiXApcnuMyPqba9NM5MgyzFZKscPXzxE',       # cohort 1 mystery
    'BSfsLADCq7q6xDh28HQWcZXqE27ZKKRz24PHsRzMxXZK',       # cohort 1 (small holder)
    'BGTuMk5RFJnGDGWgqVhggMixJ1a7HG1Yah4FqMvBZvmt',       # cohort 4
    '5Y77eogvfdQ68x7W3Hk9mmuevpjiudjWXwAEDgvPVkEa',       # cohort 3
    'sjiQ7dDVKgCM2gTWQQbVkFEeeJRGd37NKjKvZebrGs7',        # cohort 4
    'HNNt5RezThnvtPg6DtJTt4Zwot68EVCRsVukY6T2rfKH',       # cohort 6
    'JBNwDxpRX9jdseq1VMVKiB65StaTG25hDtEnL1C3eBjy',       # master hub
    '6qrp8Pv3YM9uuP4Bi17rjEsS9E8crLpfMwVLGvNRQPPr',       # funded EY7yP initial
    'FoMboHErV2g7GjfQayFCRjWdXg3USZkPJCoeYEgPastv',       # funded JB
    'HbCe8W94WroAsU9vPWgvuu9LXnDmFQdVy5w3wBXk7vve',
    '2mnaDncpw9QKqbxDcRys5gVXhqdRspWtKsuB3ciGLN77',       # FA1qn co-signer
    'EJX4Ujyr1ZgrYJSCk2Tomsk9RQopBYxJSbumyLrEj6dB',       # EY7yP co-signer
    'AbPVyaqiJTCrmhPXmSnUGcYwgCTD1XzksFeTbJgSkLd5',
    'BCo979WBjtuYXstUf1DLnWgdGGS3vQDkyF1wpLJJKBu2',
]

# Known Solana entities
KNOWN = {
    # Major CEXes (partial list)
    '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9': 'Binance hot',
    '2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm': 'Coinbase',
    '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM': 'Coinbase',
    'GhQ68iFYNB1uUgdUbUH3mqn4cGwZPWS9Hku1NX8Vpd2z': 'Kraken',
    '8BuHHAkQRPAbzmX8PcD6mg3TZo1Y5J1EcDMR1S6RbUpo': 'Kraken',
    'AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2': 'Binance',
    # Solana utility programs (appear as "source" in account-creation txs)
    '11111111111111111111111111111111': 'System program',
    # Solstice
    'DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB': 'SOLSTICE fee address',
    'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK': 'SOLSTICE presale program',
}

MAX_HOPS = 3
MIN_SOL = 0.005       # below this is dust — ignore
MAX_FUNDERS_PER_NODE = 10  # don't follow more than this
MAX_NODES = 500       # cap expansion

session = requests.Session()
tx_cache = {}

def rpc(method, params, retries=4):
    for i in range(retries):
        try:
            r = session.post(URL, json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5 * (2 ** i))); continue
            j = r.json()
            if 'error' in j:
                time.sleep(0.5); continue
            return j.get('result')
        except requests.RequestException:
            time.sleep(0.5 * (i + 1))
    return None


def fetch_tx(sig):
    if sig in tx_cache:
        return tx_cache[sig]
    tx = rpc('getTransaction', [sig, {
        'encoding': 'jsonParsed',
        'maxSupportedTransactionVersion': 0,
        'commitment': 'confirmed',
    }])
    tx_cache[sig] = tx
    return tx


def funders_for(addr, max_sigs=2000):
    """Returns list of (funder_addr, sol_in, tx_sig, blockTime) — only sig where this wallet received meaningful SOL."""
    # Collect sigs (up to max_sigs)
    sigs = []
    before = None
    while len(sigs) < max_sigs:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        b = rpc('getSignaturesForAddress', params)
        if not b: break
        sigs.extend(b); before = b[-1]['signature']
        if len(b) < 1000: break

    out = []
    def check(sig):
        tx = fetch_tx(sig['signature'])
        if not tx or (tx.get('meta') or {}).get('err'): return None
        meta = tx.get('meta') or {}
        keys = [(k.get('pubkey') if isinstance(k, dict) else k) for k in tx['transaction']['message'].get('accountKeys', [])]
        if addr not in keys: return None
        idx = keys.index(addr)
        pre = meta.get('preBalances') or []
        post = meta.get('postBalances') or []
        if idx >= len(pre) or idx >= len(post): return None
        sol_in = (post[idx] - pre[idx]) / 1e9
        if idx == 0: sol_in += (meta.get('fee') or 0) / 1e9
        if sol_in < MIN_SOL: return None
        best = None; max_drop = 0
        for i, k in enumerate(keys):
            if k == addr: continue
            if i >= len(pre) or i >= len(post): continue
            d = (pre[i] - post[i]) / 1e9
            if i == 0: d -= (meta.get('fee') or 0) / 1e9
            if d > max_drop and d > MIN_SOL:
                max_drop = d; best = k
        if best is None: return None
        return (best, sol_in, sig['signature'], sig.get('blockTime'))

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(check, s) for s in sigs]
        for f in as_completed(futs):
            r = f.result()
            if r: out.append(r)
    return out


def main():
    # Load Solstice datasets for tagging
    cohort_by = {}
    for line in open(os.path.join(ROOT, 'data/solstice_registration/accounts.jsonl')):
        d = json.loads(line)
        if '_error' in d: continue
        cohort_by[d['walletAddress']] = d.get('cohort')
    fee_payers = {x['sender'] for x in json.load(open(os.path.join(ROOT, 'data/fee_payers.json')))}
    presale = {b['sender']: b for b in json.load(open(os.path.join(ROOT, 'data/slx_presale_buyers.json')))['buyers']}

    def tag(addr):
        t = []
        if addr in KNOWN: t.append(KNOWN[addr])
        if cohort_by.get(addr): t.append(f'cohort={cohort_by[addr]}')
        if addr in fee_payers: t.append('fee-payer')
        if addr in presale: t.append(f'presale=${presale[addr]["totalUsdc"]:,.0f}')
        return t

    # BFS trace
    visited = {}   # addr -> {tags, hop, funders:[], funded:[]}
    queue = [(a, 0) for a in SEED]
    for a in SEED:
        visited[a] = {'tags': tag(a), 'hop': 0, 'funders': []}

    while queue and len(visited) < MAX_NODES:
        addr, hop = queue.pop(0)
        if hop >= MAX_HOPS: continue
        print(f'[{len(visited)}] hop={hop} tracing {addr[:12]}...', flush=True)
        fs = funders_for(addr)
        if not fs: continue
        # Aggregate per funder
        agg = defaultdict(lambda: {'total':0.0,'count':0,'first_sig':None,'first_ts':0})
        for f_addr, sol, sig, ts in fs:
            a = agg[f_addr]
            a['total'] += sol
            a['count'] += 1
            if not a['first_ts'] or (ts and ts < a['first_ts']):
                a['first_ts'] = ts or 0
                a['first_sig'] = sig
        # Keep top MAX_FUNDERS_PER_NODE by total SOL
        top = sorted(agg.items(), key=lambda x: -x[1]['total'])[:MAX_FUNDERS_PER_NODE]
        visited[addr]['funders'] = [
            {'from': f, 'total': round(d['total'],4), 'count': d['count'],
             'firstTs': d['first_ts'], 'firstSig': d['first_sig']}
            for f, d in top
        ]
        # Expand each funder into the queue if not already visited
        for f_addr, _d in top:
            if f_addr not in visited:
                visited[f_addr] = {'tags': tag(f_addr), 'hop': hop + 1, 'funders': []}
                # Don't expand beyond MAX_HOPS or if funder is a known entity
                if hop + 1 < MAX_HOPS and f_addr not in KNOWN:
                    queue.append((f_addr, hop + 1))

    out_path = os.path.join(ROOT, 'data/cluster_trace.json')
    with open(out_path, 'w') as f:
        json.dump(visited, f, separators=(',', ':'), default=str)
    print(f'\nwrote {out_path}  ({len(visited)} wallets in graph)')

    # Summary: print every wallet with tags + funders
    print('\n' + '=' * 100)
    print('CLUSTER GRAPH — each wallet, its tags, and where its SOL came from')
    print('=' * 100)
    for addr, node in sorted(visited.items(), key=lambda x: (x[1]['hop'], x[0])):
        tags = node['tags']
        hop = node['hop']
        print(f"\n[hop={hop}] {addr}")
        if tags:
            print(f"  tags: {tags}")
        for fn in node['funders'][:5]:
            f_tags = visited.get(fn['from'], {}).get('tags') or tag(fn['from'])
            import datetime as dt
            first = dt.datetime.utcfromtimestamp(fn['firstTs']).isoformat()[:10] if fn['firstTs'] else '?'
            print(f"    <- {fn['total']:>8.4f} SOL ({fn['count']}×) from {fn['from']}  {f_tags}  first {first}")


if __name__ == '__main__':
    main()
