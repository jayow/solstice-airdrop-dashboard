"""For each Solstice fee-payer wallet, check current USX/eUSX token accounts.
Flags a wallet as "ever held" if any USX/eUSX ATA exists (even with 0 balance —
Solana wallets rarely close ATAs, so ATA presence = held at some point).

Output: data/wallet_holdings.json
  { "<wallet>": {
      "usx":  { "currBal": float, "accounts": int, "held": bool },
      "eusx": { "currBal": float, "accounts": int, "held": bool },
    }, ... }
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

USX  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'

FEE_PAYERS = os.path.join(ROOT, 'data/fee_payers.json')
OUT_PATH   = os.path.join(ROOT, 'data/wallet_holdings.json')

CONCURRENCY = 20
TIMEOUT = 20
RETRIES = 6

session = requests.Session()
write_lock = threading.Lock()
progress_lock = threading.Lock()
counters = {'done': 0, 'err': 0, 'holders_usx': 0, 'holders_eusx': 0}


def rpc(method, params):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    for attempt in range(RETRIES):
        try:
            r = session.post(URL, json=body, timeout=TIMEOUT)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5 * (2 ** attempt))); continue
            r.raise_for_status()
            j = r.json()
            if 'error' in j:
                msg = str(j['error'])
                if 'rate' in msg.lower() or 'limit' in msg.lower() or '-32429' in msg:
                    time.sleep(min(8, 0.5 * (2 ** attempt))); continue
                raise RuntimeError(msg)
            return j.get('result')
        except requests.RequestException:
            time.sleep(min(4, 0.5 * (2 ** attempt)))
    raise RuntimeError(f'rpc {method} retries exhausted')


def holdings_for(wallet):
    out = {}
    for mint, sym in ((USX, 'usx'), (EUSX, 'eusx')):
        res = rpc('getTokenAccountsByOwner', [wallet, {'mint': mint}, {'encoding': 'jsonParsed'}])
        accs = (res or {}).get('value') or []
        bal = 0.0
        for a in accs:
            try:
                bal += float(a['account']['data']['parsed']['info']['tokenAmount']['uiAmount'] or 0)
            except Exception:
                pass
        out[sym] = {
            'currBal': round(bal, 4),
            'accounts': len(accs),
            'held': len(accs) > 0,
        }
    return out


def main():
    wallets = sorted({x['sender'] for x in json.load(open(FEE_PAYERS))})

    # Resume: load existing if any
    out = {}
    if os.path.exists(OUT_PATH):
        try:
            out = json.load(open(OUT_PATH))
        except Exception:
            out = {}

    todo = [w for w in wallets if w not in out]
    print(f'total: {len(wallets):,}  already done: {len(out):,}  remaining: {len(todo):,}', flush=True)

    def work(w):
        try:
            return w, holdings_for(w), None
        except Exception as e:
            return w, None, str(e)[:100]

    t0 = time.time()
    last_save = time.time()
    try:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(work, w): w for w in todo}
            for fut in as_completed(futs):
                w, data, err = fut.result()
                with write_lock:
                    if err:
                        counters['err'] += 1
                    else:
                        out[w] = data
                        if data['usx']['held']:  counters['holders_usx']  += 1
                        if data['eusx']['held']: counters['holders_eusx'] += 1
                counters['done'] += 1
                if counters['done'] % 500 == 0 or counters['done'] == len(todo):
                    dt = time.time() - t0
                    rate = counters['done'] / dt if dt else 0
                    eta = (len(todo) - counters['done']) / rate if rate else 0
                    print(f'  {counters["done"]:>6}/{len(todo)}  '
                          f'err={counters["err"]} usx-holders={counters["holders_usx"]} eusx-holders={counters["holders_eusx"]}  '
                          f'{rate:.1f}/s  eta={eta:.0f}s', flush=True)
                # periodic save
                if time.time() - last_save > 30:
                    with write_lock:
                        with open(OUT_PATH, 'w') as f:
                            json.dump(out, f, separators=(',', ':'))
                        last_save = time.time()
    finally:
        with open(OUT_PATH, 'w') as f:
            json.dump(out, f, separators=(',', ':'))
        print(f'\nwrote {OUT_PATH}', flush=True)


if __name__ == '__main__':
    main()
