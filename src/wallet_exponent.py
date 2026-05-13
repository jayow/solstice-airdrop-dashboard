#!/usr/bin/env python3
"""Wallet-centric Exponent YT trade extractor.

For each fee-payer wallet, query Helius Enhanced `GET /v0/addresses/{wallet}/transactions`
in paginated 100-tx pages, filter to txs that touch a known Exponent YT mint, and
emit one buy/sell event per market touched.

Output: data/exponent_trades_wallet.jsonl (one JSON per event).
Resumable: skips wallets whose completion is already recorded in data/wallet_cursor.json.
"""
import os, json, sys, time, threading, re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE_PAYERS = os.path.join(ROOT, 'data/fee_payers.json')
OUT_FILE = os.path.join(ROOT, 'data/exponent_trades_wallet.jsonl')
CURSOR_FILE = os.path.join(ROOT, 'data/wallet_cursor.json')

# Load API key
ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV.get('HELIUS_API_KEY', '').strip()
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW

# Markets
MARKETS = {
    'USX-09FEB26':  ('HQmMS5W34VcMtR85akhZgvypy7iqVWRXi282vwdf9eTX', '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', '4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT', 1.00),
    'USX-01JUN26':  ('Au8g11nXqXrUAmL14GM3gQnrnJnr4dcpgc5DNAnu9F9s', '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', '4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT', 1.00),
    'eUSX-11MAR26': ('DDoYyEUcdkHV5a4NCPXDRL9f93NgPbqK9ZANAGL627wF', '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', '7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR', 1.01),
    'eUSX-01JUN26': ('GEYwnvNzqFXrLnNq4riXbn2ASnwU3cF8RXW6wXKHM4sw', '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', '7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR', 1.01),
}
YT_MINTS = {v[0] for v in MARKETS.values()}

_tls = threading.local()
def _session():
    s = getattr(_tls, 'sess', None)
    if s is None:
        s = requests.Session()
        s.headers.update({'User-Agent': 'curl/8.7.1', 'Connection': 'close'})
        _tls.sess = s
    return s

def get_txs(wallet, before=None, retries=15):
    url = f'https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={KEY}&limit=100'
    if before: url += f'&before={before}'
    for i in range(retries):
        try:
            r = _session().get(url, timeout=(10, 30))  # (connect, read)
            if r.status_code in (429, 413, 403, 502, 503, 504):
                time.sleep(min(20, 1.0 * (2 ** i))); continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            time.sleep(min(20, 1.0 * (2 ** i)))
    raise RuntimeError('retries exhausted')

def classify(t, wallet):
    if t.get('transactionError'): return []
    transfers = t.get('tokenTransfers') or []
    mints = {tr.get('mint') for tr in transfers}
    hits = [(k, v) for k, v in MARKETS.items() if v[0] in mints]
    if not hits: return []
    signer = t.get('feePayer')
    # Only count if this wallet was the one paying (signer); otherwise ignore
    if signer != wallet: return []
    events = []
    for k, (yt, und, sy, px) in hits:
        def delta(mint):
            d = 0.0
            for tr in transfers:
                if tr.get('mint') != mint: continue
                if tr.get('toUserAccount') == wallet: d += float(tr.get('tokenAmount') or 0)
                if tr.get('fromUserAccount') == wallet: d -= float(tr.get('tokenAmount') or 0)
            return d
        u = delta(und); syd = delta(sy); yd = delta(yt)
        usd = (u + syd) * px
        action = 'other'
        if yd > 1e-4: action = 'buyYt'
        elif yd < -1e-4: action = 'sellYt'
        elif u < -1e-4 or syd < -1e-4: action = 'buyYt'
        elif u > 1e-4 or syd > 1e-4: action = 'sellYt'
        events.append({
            'sig': t['signature'], 'blockTime': t.get('timestamp'),
            'market': k, 'signer': wallet, 'action': action,
            'ytDelta': round(yd, 6), 'underlyingDelta': round(u, 6), 'syDelta': round(syd, 6),
            'usdNet': round(usd, 4),
        })
    return events

MAX_PAGES = 30     # 3000 txs/wallet should cover virtually every fee-payer
# Stop paginating once we see N consecutive pages older than this cutoff
# (Exponent USX/eUSX markets didn't exist before mid-Oct 2025)
MIN_BLOCK_TIME = 1728000000  # 2024-10-04; anything before is definitely pre-Exponent
def scan_wallet(wallet):
    before = None
    events = []
    for page_idx in range(MAX_PAGES):
        page = get_txs(wallet, before=before)
        if not page: break
        for t in page:
            events.extend(classify(t, wallet))
        oldest_time = page[-1].get('timestamp') or 0
        before = page[-1]['signature']
        if len(page) < 100: break
        if oldest_time and oldest_time < MIN_BLOCK_TIME: break
    return events

def load_cursor():
    if not os.path.exists(CURSOR_FILE): return {}
    try: return json.load(open(CURSOR_FILE))
    except: return {}

def save_cursor(c):
    tmp = CURSOR_FILE + '.tmp'
    json.dump(c, open(tmp, 'w'))
    os.replace(tmp, CURSOR_FILE)

def main():
    payers = json.load(open(FEE_PAYERS))
    # Only keep legit (non-dust) wallets
    wallets = [p['sender'] for p in payers if p['totalSOL'] >= 0.001 or p['totalUSDC'] >= 0.01 or p['totalUSDT'] >= 0.01]
    cursor = load_cursor()
    pending = [w for w in wallets if w not in cursor]
    print(f'Fee-payers (legit): {len(wallets)}, already done: {len(wallets) - len(pending)}, pending: {len(pending)}', flush=True)

    out = open(OUT_FILE, 'a')
    lock = threading.Lock()
    state = {'done': len(cursor), 'total': len(wallets), 'with_activity': 0, 'events': 0, 'start': time.time()}

    def work(w):
        try:
            evs = scan_wallet(w)
        except Exception as e:
            with lock: sys.stdout.write(f'\n[{w[:8]}] err: {e}\n'); sys.stdout.flush()
            return
        with lock:
            for ev in evs:
                out.write(json.dumps(ev) + '\n')
            out.flush()
            cursor[w] = {'events': len(evs)}
            state['done'] += 1
            if evs:
                state['with_activity'] += 1
                state['events'] += len(evs)
            if state['done'] % 10 == 0:
                rate = state['done'] / max(time.time() - state['start'], 1)
                eta = (state['total'] - state['done']) / max(rate, 0.001)
                sys.stdout.write(f"\r{state['done']}/{state['total']}  active={state['with_activity']}  events={state['events']}  rate={rate:.1f}/s  eta={eta/60:.1f}m")
                sys.stdout.flush()
            if state['done'] % 100 == 0:
                save_cursor(cursor)

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(work, w) for w in pending]
        for f in as_completed(futs):
            f.result()

    save_cursor(cursor)
    out.close()
    print(f"\nDone. {state['done']}/{state['total']}  wallets-with-activity={state['with_activity']}  total-events={state['events']}")

if __name__ == '__main__':
    main()
