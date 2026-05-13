"""Reconstruct USX / eUSX holding timeline for a handful of wallets.

For each target wallet:
  1. Fetch all its transaction signatures (paginated).
  2. For each sig, fetch the parsed transaction and extract any balance delta
     on a token account owned by the wallet with mint = USX or eUSX.
  3. Sort by time, reconstruct running balance, compute:
       firstHold, lastHold, daysHeld, maxBal, balDays (∫bal·dt), netFlow

Writes data/wallet_holdings_debug.json and prints a human-readable summary.
"""
import os, json, sys, time, threading
import datetime as dt
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    line = line.strip()
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
MINT_TO_SYM = {USX_MINT: 'USX', EUSX_MINT: 'eUSX'}

WALLETS = [
    'BSfsLADCq7q6xDh28HQWcZXqE27ZKKRz24PHsRzMxXZK',
    'FA1qnqZWMptKtmyQ4DQvufH5tiYentEyvStPehsJfCTk',
    'EY7yPT1nJr7AWiXApcnuMyPqba9NM5MgyzFZKscPXzxE',
    '45KiDVz4eud4zLk2nDvdF6SzCQiChF1AAPUKBCA35mVW',
    '6Asjwpu4oMGTC4eFamFfd34UxAe52wSQEWuErYCqGe3N',
    'GmHqZbi9kZmVBrWDkscDyZHVb7EqnZmn28QNc1m1nXBR',
    '8miKdCPti1ZXZKtk1GEtB8dnt1cnWt6BGmTY9Utpgmrq',
]

CONCURRENCY = 8
session = requests.Session()


def rpc(method, params, retries=8):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    for i in range(retries):
        try:
            r = session.post(URL, json=body, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5 * (2 ** i))); continue
            r.raise_for_status()
            j = r.json()
            if 'error' in j:
                msg = str(j['error'])
                if 'rate' in msg.lower() or 'limit' in msg.lower() or '-32429' in msg:
                    time.sleep(min(8, 0.5 * (2 ** i))); continue
                raise RuntimeError(msg)
            return j.get('result')
        except requests.RequestException:
            time.sleep(min(4, 0.5 * (2 ** i)))
    raise RuntimeError(f'rpc {method} retries exhausted')


def fetch_all_sigs(addr):
    """All sigs for an address — walk back until empty."""
    out = []
    before = None
    while True:
        params = [addr, {'limit': 1000}]
        if before:
            params[1]['before'] = before
        batch = rpc('getSignaturesForAddress', params)
        if not batch:
            break
        out.extend(batch)
        before = batch[-1]['signature']
        if len(batch) < 1000:
            break
    return out


def fetch_tx_delta(sig, wallet):
    """Return list of (mint_symbol, delta_float) for this tx, on token accounts owned by wallet.
       Empty if tx has no USX/eUSX movement for this wallet."""
    try:
        tx = rpc('getTransaction', [sig, {
            'encoding': 'jsonParsed',
            'maxSupportedTransactionVersion': 0,
            'commitment': 'confirmed',
        }])
    except Exception as e:
        return [], str(e)[:80]
    if not tx or tx.get('meta', {}).get('err'):
        return [], None
    meta = tx['meta']
    pre = meta.get('preTokenBalances') or []
    post = meta.get('postTokenBalances') or []

    # Index by accountIndex
    def bal_for(lst, idx, mint):
        for b in lst:
            if b['accountIndex'] == idx and b.get('mint') == mint and b.get('owner') == wallet:
                return float(b['uiTokenAmount']['uiAmount'] or 0)
        return None

    # Collect account indices that are USX/eUSX for this wallet (pre or post)
    indices = set()
    for b in pre + post:
        if b.get('owner') == wallet and b.get('mint') in MINT_TO_SYM:
            indices.add((b['accountIndex'], b['mint']))

    out = []
    for idx, mint in indices:
        prev = bal_for(pre, idx, mint) or 0.0
        curr = bal_for(post, idx, mint) or 0.0
        d = curr - prev
        if abs(d) > 1e-9:
            out.append((MINT_TO_SYM[mint], d, curr))
    return out, None


def investigate(wallet):
    sigs = fetch_all_sigs(wallet)
    print(f'  {wallet}: {len(sigs):,} total sigs', flush=True)

    # Parallel tx fetches
    deltas_by_sig = {}
    errs = 0

    def work(s):
        return s['signature'], s.get('blockTime') or 0, fetch_tx_delta(s['signature'], wallet)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(work, s) for s in sigs]
        for i, fut in enumerate(as_completed(futs)):
            sig, bt, (ds, err) = fut.result()
            if err:
                errs += 1; continue
            if ds:
                deltas_by_sig[sig] = (bt, ds)

    # Flatten events and sort
    events = []
    for sig, (bt, ds) in deltas_by_sig.items():
        for sym, delta, post_bal in ds:
            events.append((bt, sym, delta, post_bal, sig))
    events.sort(key=lambda e: e[0])

    # Reconstruct per-mint timeline
    result = {}
    for sym in ('USX', 'eUSX'):
        series = [e for e in events if e[1] == sym]
        if not series:
            result[sym] = None; continue
        bal = 0.0
        max_bal = 0.0
        first_nonzero = None
        last_nonzero = None
        bal_days = 0.0
        prev_ts = series[0][0]
        prev_bal = 0.0
        net_received = 0.0
        net_sent = 0.0
        for bt, _, delta, post, _ in series:
            # accumulate balance-days using previous balance up to this tx
            if prev_bal > 0:
                bal_days += prev_bal * (bt - prev_ts) / 86400.0
            bal = post
            if delta > 0: net_received += delta
            else:         net_sent += -delta
            max_bal = max(max_bal, bal)
            if bal > 0.0001:
                if first_nonzero is None: first_nonzero = bt
                last_nonzero = bt
            prev_ts = bt
            prev_bal = bal
        # tail: balance-days from last tx to now
        now = int(time.time())
        if prev_bal > 0:
            bal_days += prev_bal * (now - prev_ts) / 86400.0

        days_held = 0
        if first_nonzero and last_nonzero:
            days_held = max(1, (last_nonzero - first_nonzero) // 86400)

        result[sym] = {
            'events': len(series),
            'maxBal': round(max_bal, 4),
            'currBal': round(bal, 4),
            'netReceived': round(net_received, 4),
            'netSent': round(net_sent, 4),
            'netFlow': round(net_received - net_sent, 4),
            'firstHoldTs': first_nonzero,
            'lastHoldTs': last_nonzero,
            'daysHeld': int(days_held),
            'balDays': round(bal_days, 2),
        }
    return {'totalSigs': len(sigs), 'txsWithMovement': len(deltas_by_sig), 'errs': errs, 'mints': result}


def main():
    print(f'RPC: {URL.split("api-key=")[-1][:8]}...')
    print(f'Investigating {len(WALLETS)} wallets\n')

    all_out = {}
    t0 = time.time()
    for w in WALLETS:
        print(f'\n=== {w} ===')
        d = investigate(w)
        all_out[w] = d
        mints = d['mints']
        for sym in ('USX', 'eUSX'):
            m = mints.get(sym)
            if not m:
                print(f'  {sym}: no movement'); continue
            first = dt.datetime.utcfromtimestamp(m['firstHoldTs']).isoformat()[:10] if m['firstHoldTs'] else '?'
            last  = dt.datetime.utcfromtimestamp(m['lastHoldTs']).isoformat()[:10]  if m['lastHoldTs']  else '?'
            print(f'  {sym}: events={m["events"]}  max={m["maxBal"]:>12,.2f}  curr={m["currBal"]:>12,.2f}  '
                  f'held {m["daysHeld"]}d ({first} → {last})  balDays={m["balDays"]:.0f}')

    out_path = os.path.join(ROOT, 'data/wallet_holdings_debug.json')
    with open(out_path, 'w') as f:
        json.dump(all_out, f, separators=(',', ':'), default=str)
    print(f'\nwrote {out_path} in {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
