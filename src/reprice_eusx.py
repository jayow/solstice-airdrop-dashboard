#!/usr/bin/env python3
"""Apply time-accurate eUSX→USX exchange rate to all eUSX YT trades.

For each UTC day that has at least one eUSX trade, we fetch one raw tx from
that day and parse the log line `Program log: sy exchange rate: N` (where
N/1e12 is the eUSX→USX ratio at that moment). We cache {day → rate} to
data/eusx_rates.json, then rewrite the usdNet field of every eUSX event in
data/exponent_trades.jsonl in-place.

Fee credits used: ~1 RPC `getTransaction` per day of eUSX activity (~150).
"""
import os, json, re, time, threading, datetime, sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, 'data/exponent_trades.jsonl')
RATES  = os.path.join(ROOT, 'data/eusx_rates.json')

ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV['HELIUS_API_KEY']
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW
RPC_URL = f'https://mainnet.helius-rpc.com/?api-key={KEY}'

EUSX_MARKETS = {'eUSX-11MAR26', 'eUSX-01JUN26'}

def day_of(ts):
    return int(ts // 86400)

def ts_to_day_str(day):
    return datetime.datetime.utcfromtimestamp(day * 86400).strftime('%Y-%m-%d')

def get_sy_rate(sig, retries=8):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',
            'params': [sig, {'encoding': 'json', 'maxSupportedTransactionVersion': 0}]}
    for i in range(retries):
        try:
            r = requests.post(RPC_URL, json=body,
                              headers={'Content-Type': 'application/json', 'User-Agent': 'curl/8.7.1'},
                              timeout=30)
            if r.status_code in (429, 413, 503, 504):
                time.sleep(min(8, 0.5*(2**i))); continue
            j = r.json()
            if j.get('error'):
                msg = j['error'].get('message','')
                if 'rate' in msg.lower() or 'too many' in msg.lower():
                    time.sleep(min(8, 0.5*(2**i))); continue
                return None
            logs = j['result']['meta'].get('logMessages', []) or []
            for l in logs:
                m = re.search(r'sy exchange rate:\s*(\d+)', l)
                if m:
                    return int(m.group(1)) / 1e12
            return None
        except Exception:
            time.sleep(min(8, 0.5*(2**i)))
    return None

def main():
    # Load all eUSX events
    events = []
    with open(TRADES) as f:
        for l in f:
            try: r = json.loads(l)
            except: continue
            if r.get('market') in EUSX_MARKETS and r.get('blockTime'):
                events.append(r)
    print(f'eUSX trade events: {len(events)}', flush=True)

    # Pick one sig per day (prefer one with a good underlyingDelta for reliability)
    day_sigs = {}   # day -> sig
    for e in events:
        d = day_of(e['blockTime'])
        if d not in day_sigs:
            day_sigs[d] = e['sig']
    print(f'unique days: {len(day_sigs)}', flush=True)

    # Load any cached rates
    cache = {}
    if os.path.exists(RATES):
        try: cache = {int(k): v for k, v in json.load(open(RATES)).items()}
        except: cache = {}
    pending = [(d, s) for d, s in day_sigs.items() if d not in cache]
    print(f'cached: {len(cache)}, to fetch: {len(pending)}', flush=True)

    lock = threading.Lock()
    state = {'done': 0, 'ok': 0}

    def fetch(day, sig):
        rate = get_sy_rate(sig)
        with lock:
            state['done'] += 1
            if rate is not None:
                cache[day] = rate
                state['ok'] += 1
            if state['done'] % 10 == 0:
                json.dump({str(k): v for k, v in cache.items()}, open(RATES, 'w'))
                sys.stdout.write(f"\r{state['done']}/{len(pending)}  ok={state['ok']}")
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch, d, s) for d, s in pending]
        for f in as_completed(futs): f.result()
    json.dump({str(k): v for k, v in cache.items()}, open(RATES, 'w'))
    print(f"\nrates fetched: {state['ok']}/{len(pending)}")

    # Fill any missing days by nearest-neighbour interpolation
    rates = dict(cache)
    if rates:
        all_days = sorted(rates.keys())
        for d in sorted(day_sigs.keys()):
            if d not in rates:
                # nearest day
                nearest = min(all_days, key=lambda x: abs(x - d))
                rates[d] = rates[nearest]
    print(f'rates available for {len(rates)} days (range {ts_to_day_str(min(rates))} → {ts_to_day_str(max(rates))})')
    print(f'rate range: {min(rates.values()):.6f} .. {max(rates.values()):.6f}')

    # Rewrite exponent_trades.jsonl: apply rate to eUSX events only
    updated = 0
    new_lines = []
    with open(TRADES) as f:
        for l in f:
            l = l.strip()
            if not l: continue
            try: r = json.loads(l)
            except: new_lines.append(l); continue
            if r.get('market') in EUSX_MARKETS and r.get('blockTime'):
                d = day_of(r['blockTime'])
                rate = rates.get(d)
                if rate:
                    u = r.get('underlyingDelta', 0)
                    sy = r.get('syDelta', 0)
                    r['eusxRate'] = round(rate, 6)
                    r['usdNet'] = round((u + sy) * rate, 4)
                    updated += 1
            new_lines.append(json.dumps(r))
    with open(TRADES, 'w') as f:
        f.write('\n'.join(new_lines) + '\n')
    print(f'Rewrote {updated} eUSX events with time-accurate rates.')

if __name__ == '__main__':
    main()
