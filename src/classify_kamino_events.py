#!/usr/bin/env python3
"""Classify Kamino events into supply/withdraw/borrow/repay from tx logs.
Same pattern as classify_events.py for Exponent.
"""
import os, json, re, time, threading, sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from kamino_markets import KAMINO_LEND_PROGRAM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_IN = os.path.join(ROOT, 'data/kamino_events.jsonl')
CACHE = os.path.join(ROOT, 'data/kamino_sig_instr.json')

ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV['HELIUS_API_KEY']
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW
RPC_URL = f'https://mainnet.helius-rpc.com/?api-key={KEY}'

# Kamino Lend instruction → action (case-insensitive lookup applied below)
_INSTR_MAP = {
    'depositreserveliquidity':                              'supply',
    'depositreserveliquidityandobligationcollateral':        'supply',
    'depositreserveliquidityandobligationcollateralv2':      'supply',
    'depositobligationcollateral':                           'supply',
    'redeemreservecollateral':                               'withdraw',
    'withdrawobligationcollateral':                          'withdraw',
    'withdrawobligationcollateralandredeemreservecollateral':'withdraw',
    'withdrawobligationcollateralandredeemreservecollateralv2':'withdraw',
    'borrowobligationliquidity':                             'borrow',
    'borrowobligationliquidityv2':                           'borrow',
    'repayobligationliquidity':                              'repay',
    'repayobligationliquidityv2':                            'repay',
    'flashborrowreserveliquidity':                           'flashBorrow',
    'flashrepayreserveliquidity':                            'flashRepay',
    'liquidateobligationandredeemreservecollateral':         'liquidation',
    'liquidateobligationandredeemreservecollateralv2':       'liquidation',
}
def INSTR_TO_ACTION_GET(name):
    if not name: return None
    return _INSTR_MAP.get(name.lower())

def get_instr(sig, retries=8):
    body = {'jsonrpc':'2.0','id':1,'method':'getTransaction',
            'params':[sig, {'encoding':'json','maxSupportedTransactionVersion':0}]}
    for i in range(retries):
        try:
            r = requests.post(RPC_URL, json=body,
                              headers={'Content-Type':'application/json','User-Agent':'curl/8.7.1'}, timeout=30)
            if r.status_code in (429, 413, 503, 504):
                time.sleep(min(8, 0.5*(2**i))); continue
            j = r.json()
            if j.get('error'):
                msg = j['error'].get('message','')
                if 'max usage' in msg.lower() or 'too many' in msg.lower():
                    time.sleep(min(8, 0.5*(2**i))); continue
                return None
            logs = j['result']['meta'].get('logMessages',[]) or []
            # Find the first non-housekeeping Kamino instruction.
            # RefreshReserve, RefreshObligation etc. are CPI prerequisites called
            # before every real action — skip them.
            SKIP = {'RefreshReserve','RefreshObligation','RefreshReservesBatch',
                    'InitObligation','InitUserMetadata','InitObligationFarmsForReserve',
                    'InitReserve'}
            saw = False
            for l in logs:
                if f'Program {KAMINO_LEND_PROGRAM} invoke' in l:
                    saw = True; continue
                if saw:
                    m = re.search(r'Instruction:\s*(\w+)', l)
                    if m:
                        name = m.group(1)
                        if name in SKIP:
                            saw = False; continue  # skip and look for next invoke
                        return name
                    if l.startswith('Program ') and 'success' in l:
                        saw = False
            return None
        except Exception:
            time.sleep(min(8, 0.5*(2**i)))
    return None

def main():
    sigs = set()
    with open(TRADES_IN) as f:
        for l in f:
            try: r = json.loads(l)
            except: continue
            if r.get('reserve'): sigs.add(r['sig'])
    print(f'Unique sigs: {len(sigs)}', flush=True)

    cache = {}
    if os.path.exists(CACHE):
        try: cache = json.load(open(CACHE))
        except: cache = {}
    pending = [s for s in sigs if s not in cache]
    print(f'Cached: {len(cache)}, to fetch: {len(pending)}', flush=True)

    lock = threading.Lock()
    state = {'done': 0, 'ok': 0, 'start': time.time()}
    def fetch(sig):
        inst = get_instr(sig)
        with lock:
            state['done'] += 1
            if inst:
                cache[sig] = inst
                state['ok'] += 1
            if state['done'] % 100 == 0:
                json.dump(cache, open(CACHE,'w'))
                rate = state['done']/max(time.time()-state['start'],1)
                eta = (len(pending)-state['done'])/max(rate,0.001)
                sys.stdout.write(f"\r{state['done']}/{len(pending)} ok={state['ok']} rate={rate:.1f}/s eta={eta/60:.1f}m")
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=25) as ex:
        futs = [ex.submit(fetch, s) for s in pending]
        for f in as_completed(futs): f.result()
    json.dump(cache, open(CACHE,'w'))
    print(f"\nDone. cached={len(cache)}", flush=True)

    # Re-classify
    out_lines = []
    counts = {}
    with open(TRADES_IN) as f:
        for l in f:
            l = l.strip()
            if not l: continue
            try: r = json.loads(l)
            except: out_lines.append(l); continue
            if r.get('reserve'):
                inst = cache.get(r['sig'])
                r['instr'] = inst
                action = INSTR_TO_ACTION_GET(inst)
                if action:
                    r['action'] = action
                else:
                    if r['action'] == 'inflow': r['action'] = 'withdraw'
                    elif r['action'] == 'outflow': r['action'] = 'supply'
                counts[r['action']] = counts.get(r['action'], 0) + 1
            out_lines.append(json.dumps(r))
    with open(TRADES_IN, 'w') as f:
        f.write('\n'.join(out_lines) + '\n')
    print('Final event classification:')
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'  {k}: {v}')

if __name__ == '__main__':
    main()
