"""Scrape all USDC flow in/out of the Solstice SLX presale program,
extract per-wallet NET USDC contribution (deposits minus refunds).

Output: data/slx_presale_buyers.json
"""
import os, json, time, threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

PRESALE = 'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK'
USDC    = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
OUT     = os.path.join(ROOT, 'data/slx_presale_buyers.json')

CONCURRENCY = 12
session = requests.Session()


def rpc(method, params, retries=6):
    body = {'jsonrpc':'2.0','id':1,'method':method,'params':params}
    for i in range(retries):
        try:
            r = session.post(URL, json=body, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5*(2**i))); continue
            r.raise_for_status()
            j = r.json()
            if 'error' in j:
                msg = str(j['error'])
                if 'rate' in msg.lower() or '-32429' in msg:
                    time.sleep(min(8, 0.5*(2**i))); continue
                raise RuntimeError(msg)
            return j.get('result')
        except requests.RequestException:
            time.sleep(min(4, 0.5*(2**i)))
    raise RuntimeError(f'rpc {method} retries exhausted')


def fetch_all_sigs():
    out = []
    before = None
    while True:
        params = [PRESALE, {'limit': 1000}]
        if before: params[1]['before'] = before
        batch = rpc('getSignaturesForAddress', params)
        if not batch: break
        out.extend(batch)
        before = batch[-1]['signature']
        print(f'  sigs so far: {len(out):,}  oldest: {time.strftime("%Y-%m-%d", time.gmtime(batch[-1].get("blockTime") or 0))}', flush=True)
        if len(batch) < 1000: break
    return out


def extract_deltas(sig):
    """Return dict of {owner: usdc_delta} + blockTime for this tx, or None."""
    tx = rpc('getTransaction', [sig, {
        'encoding': 'jsonParsed',
        'maxSupportedTransactionVersion': 0,
        'commitment': 'confirmed',
    }])
    if not tx or (tx.get('meta') or {}).get('err'):
        return None, None
    meta = tx.get('meta') or {}
    pre  = meta.get('preTokenBalances') or []
    post = meta.get('postTokenBalances') or []
    bt = tx.get('blockTime')

    def per_idx(lst):
        out = {}
        for b in lst:
            if b.get('mint') != USDC: continue
            out[b['accountIndex']] = {
                'owner': b.get('owner'),
                'amt': float(b['uiTokenAmount']['uiAmount'] or 0),
            }
        return out

    pre_map = per_idx(pre)
    post_map = per_idx(post)
    all_idx = set(pre_map) | set(post_map)

    deltas = {}
    for i in all_idx:
        p = pre_map.get(i); q = post_map.get(i)
        owner = (q or p).get('owner')
        if not owner: continue
        d = (q or {'amt': 0})['amt'] - (p or {'amt': 0})['amt']
        if abs(d) < 1e-9: continue
        deltas[owner] = deltas.get(owner, 0) + d
    return deltas, bt


def main():
    print('Phase 1 — fetching all presale sigs...')
    sigs = fetch_all_sigs()
    print(f'  total sigs: {len(sigs):,}\n')

    print('Phase 2 — fetching USDC deltas for each tx...')
    raw = []  # list of (deltas, blockTime)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(extract_deltas, s['signature']): s for s in sigs}
        for fut in as_completed(futs):
            done += 1
            try:
                deltas, bt = fut.result()
                if deltas:
                    raw.append((deltas, bt))
            except Exception:
                pass
            if done % 500 == 0 or done == len(sigs):
                el = time.time() - t0
                print(f'  {done:>5}/{len(sigs)}  txs with USDC: {len(raw)}  {done/el:.1f}/s', flush=True)

    # Identify vault owners — those that appear in MANY txs (counterparty to buyers).
    # Real buyers typically deposit once (2-3x max); vault appears in nearly every tx.
    from collections import Counter
    owner_tx_counts = Counter()
    for deltas, _ in raw:
        for owner in deltas.keys():
            owner_tx_counts[owner] += 1
    # Any owner present in >= 5% of txs with USDC movement is treated as a vault.
    vault_threshold = max(20, int(0.05 * len(raw)))
    vault_owners = {o for o, n in owner_tx_counts.items() if n >= vault_threshold}
    print(f'  detected {len(vault_owners)} vault owner(s) (threshold={vault_threshold} txs):')
    for o in sorted(vault_owners, key=lambda x: -owner_tx_counts[x]):
        print(f'    {owner_tx_counts[o]:>5} txs  {o}')

    # Now extract buyer flows — everyone NOT in vault set.
    flows = []
    for deltas, bt in raw:
        for owner, d in deltas.items():
            if owner in vault_owners: continue
            flows.append((owner, d, bt))

    # Aggregate per-wallet NET: delta < 0 means they deposited to presale; we want deposit magnitude
    agg = {}
    for owner, d, bt in flows:
        a = agg.setdefault(owner, {'sender': owner, 'deposited': 0.0, 'refunded': 0.0,
                                   'txCount': 0, 'firstTs': 0, 'lastTs': 0})
        if d < 0:
            a['deposited'] += -d
        else:
            a['refunded'] += d
        a['txCount'] += 1
        if bt:
            if not a['firstTs'] or bt < a['firstTs']: a['firstTs'] = bt
            if bt > a['lastTs']: a['lastTs'] = bt

    # Keep every wallet that DEPOSITED anything (even if fully refunded later).
    # Refund-only addresses (deposited = 0) are the presale counterparty PDAs — drop those.
    buyers = []
    for v in agg.values():
        net = v['deposited'] - v['refunded']
        v['totalUsdc']   = round(net, 2)
        v['deposited']   = round(v['deposited'], 2)
        v['refunded']    = round(v['refunded'], 2)
        if v['deposited'] > 0.01:
            buyers.append(v)

    buyers.sort(key=lambda x: -x['totalUsdc'])

    gross_deposits = round(sum(b['deposited'] for b in buyers), 2)
    gross_refunds  = round(sum(b['refunded']  for b in buyers), 2)
    net_usdc       = round(sum(b['totalUsdc'] for b in buyers), 2)
    net_positive   = sum(1 for b in buyers if b['totalUsdc'] > 0.01)

    summary = {
        'presaleProgram': PRESALE,
        'totalFlows': len(flows),
        'allDepositors': len(buyers),
        'uniqueBuyers': net_positive,
        'grossDeposits': gross_deposits,
        'grossRefunds': gross_refunds,
        'netUsdc': net_usdc,
        'totalUsdc': net_usdc,
        'totalRefunded': gross_refunds,
        'buyers': buyers,
    }
    with open(OUT, 'w') as f:
        json.dump(summary, f, separators=(',', ':'))
    print(f'\nwrote {OUT}')
    print(f'unique net-depositors: {summary["uniqueBuyers"]:,}  net USDC: ${summary["totalUsdc"]:,.0f}  '
          f'refunded (across buyers): ${summary["totalRefunded"]:,.0f}')
    # Buyers with non-zero refunds
    refunded = [b for b in buyers if b['refunded'] > 0.01]
    print(f'\nbuyers who got some refund: {len(refunded):,}')
    for b in refunded[:10]:
        print(f'  net=${b["totalUsdc"]:>10,.0f}  dep=${b["deposited"]:>10,.0f}  ref=${b["refunded"]:>10,.0f}  {b["sender"]}')
    print(f'\nTop 10 buyers by NET deposit:')
    import datetime as dt2
    for b in buyers[:10]:
        first = dt2.datetime.utcfromtimestamp(b['firstTs']).isoformat()[:10] if b['firstTs'] else '?'
        print(f'  ${b["totalUsdc"]:>10,.0f}  {b["sender"]}  ({b["txCount"]} tx, first {first})')


if __name__ == '__main__':
    main()
