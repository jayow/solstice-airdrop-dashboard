#!/usr/bin/env python3
"""Scrape all sigs for each Kamino Solstice-Market reserve account.
Resumable via data/kamino_sigs.cursor.json.
Writes data/kamino_sigs.json (unique by signature, sorted newest → oldest).
"""
import os, json, re, time
import requests
from kamino_markets import RESERVES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data/kamino_sigs.json')
CURSOR = os.path.join(ROOT, 'data/kamino_sigs.cursor.json')

ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV['HELIUS_API_KEY']
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW
RPC = f'https://mainnet.helius-rpc.com/?api-key={KEY}'

def rpc_call(method, params, retries=15):
    body = {'jsonrpc':'2.0','id':1,'method':method,'params':params}
    for i in range(retries):
        try:
            r = requests.post(RPC, json=body, headers={'User-Agent':'curl/8.7.1'}, timeout=30)
            if r.status_code in (429, 413, 503, 504):
                time.sleep(min(4, 0.3*(2**i))); continue
            j = r.json()
            if j.get('error'):
                msg = j['error'].get('message','')
                code = j['error'].get('code')
                if code in (-32429, -32413) or 'too many requests' in msg.lower() or 'max usage' in msg.lower():
                    time.sleep(min(4, 0.3*(2**i))); continue
                raise RuntimeError(j['error'])
            return j.get('result')
        except requests.exceptions.RequestException:
            time.sleep(min(4, 0.3*(2**i)))
    raise RuntimeError('retries exhausted')

def load_existing():
    if not os.path.exists(OUT): return {}
    try:
        arr = json.load(open(OUT))
        return {s['signature']: s for s in arr}
    except: return {}

def load_cursor():
    if not os.path.exists(CURSOR): return {}
    try: return json.load(open(CURSOR))
    except: return {}

def save(by_key, cursors):
    arr = sorted(by_key.values(), key=lambda s: -(s.get('blockTime') or 0))
    json.dump(arr, open(OUT,'w'))
    json.dump(cursors, open(CURSOR,'w'), indent=2)

def sigs_for(address, by_key, cursors):
    before = cursors.get(address, {}).get('before')
    done = cursors.get(address, {}).get('done', False)
    if done:
        print(f'  (already complete)', flush=True)
        return
    pages = 0
    while True:
        params = [address, {'limit': 1000}]
        if before: params[1]['before'] = before
        try:
            page = rpc_call('getSignaturesForAddress', params)
        except Exception as e:
            print(f'\n  PAGING STOPPED ({e}). Will resume next run.', flush=True)
            cursors[address] = {'before': before, 'done': False}
            save(by_key, cursors); return
        if not page: break
        for s in page:
            if s.get('err'): continue
            if s['signature'] not in by_key:
                by_key[s['signature']] = {'signature': s['signature'], 'blockTime': s.get('blockTime')}
        before = page[-1]['signature']
        pages += 1
        import datetime
        oldest = datetime.datetime.fromtimestamp(page[-1]['blockTime'], datetime.timezone.utc).isoformat() if page[-1].get('blockTime') else '?'
        print(f'    page {pages}: total {len(by_key)} sigs (addr oldest={oldest})', flush=True)
        cursors[address] = {'before': before, 'done': False}
        save(by_key, cursors)
        if len(page) < 1000: break
    cursors[address] = {'before': before, 'done': True}
    save(by_key, cursors)

def main():
    by_key = load_existing()
    cursors = load_cursor()
    print(f'Start: {len(by_key)} sigs on disk', flush=True)
    for sym, r in RESERVES.items():
        print(f'[{sym}] scraping {r["reserve"]}', flush=True)
        sigs_for(r['reserve'], by_key, cursors)
    save(by_key, cursors)
    arr = sorted(by_key.values(), key=lambda s: s.get('blockTime') or 0)
    print(f'\nTotal unique Kamino sigs: {len(by_key)}')
    if arr:
        import datetime
        print(f'Range: {datetime.datetime.fromtimestamp(arr[0]["blockTime"], datetime.timezone.utc).isoformat()} → {datetime.datetime.fromtimestamp(arr[-1]["blockTime"], datetime.timezone.utc).isoformat()}')

if __name__ == '__main__':
    main()
