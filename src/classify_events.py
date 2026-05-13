#!/usr/bin/env python3
"""Classify every Exponent YT event by its real instruction (from tx logs).

Current events in data/exponent_trades.jsonl use a delta-based heuristic that
confuses LP-provision with pure YT buys. This script fetches each tx's raw
logMessages via standard RPC and records the first Exponent `Instruction:`
found. It then rewrites each event with:
  - `instr`:    the on-chain instruction name (e.g. WrapperBuyYt, WrapperProvideLiquidity)
  - `action`:   buyYt | sellYt | addLiq | removeLiq | other
so the reporter can split YT buys from LP activity.

Cost: 1 RPC credit per unique sig (~48K).
Resumable: caches {sig: instr} to data/sig_instr.json.
"""
import os, json, re, time, threading, sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_IN = os.path.join(ROOT, 'data/exponent_trades.jsonl')
CACHE = os.path.join(ROOT, 'data/sig_instr.json')

ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV['HELIUS_API_KEY']
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW
RPC_URL = f'https://mainnet.helius-rpc.com/?api-key={KEY}'

EXPONENT = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'

# Map top-level Exponent instruction -> action used for reporting.
# Verified empirically against Exponent UI rows on a known wallet.
INSTR_TO_ACTION = {
    # YT buys
    'WrapperBuyYt':                    'buyYt',
    'BuyYt':                           'buyYt',
    'InitializeYieldPosition':         'buyYt',   # first YT acquisition on a market
    # YT sells (includes WithdrawYt which inner-calls WrapperSellYt + Burn)
    'WrapperSellYt':                   'sellYt',
    'SellYt':                          'sellYt',
    'WithdrawYt':                      'sellYt',
    # LP add
    'WrapperProvideLiquidity':         'addLiq',
    'WrapperProvideLiquidityBase':     'addLiq',   # SY+PT pure-LP flow
    'WrapperProvideLiquidityYt':       'addLiq',
    'InitLpPosition':                  'addLiq',   # first-time LP deposit
    'ProvideLiquidity':                'addLiq',
    'MarketTwoDepositLiquidity':       'addLiq',
    'MarketDepositLp':                 'addLiq',
    # LP remove
    'WrapperWithdrawLiquidity':        'removeLiq',
    'WrapperWithdrawLiquidityClassic': 'removeLiq',
    'WrapperRemoveLiquidity':          'removeLiq',
    'MarketWithdrawLp':                'removeLiq',
    'RemoveLiquidity':                 'removeLiq',
    # Not YT-relevant (PT-only trades, position mgmt, admin) -> excluded from YT totals
    'WrapperBuyPt':                    'other',
    'WrapperSellPt':                   'other',
    'WrapperMerge':                    'other',    # maturity PT redemption
    'Strip':                           'other',
    'InitMarketTwo':                   'other',
    # Yield / emission claims (user collects accrued interest + Solstice Flares)
    'WrapperCollectInterest':          'claimYield',
    'CollectInterest':                 'claimYield',
    'StageYtYield':                    'claimYield',
    'CollectEmission':                 'claimYield',
}

def get_instr(sig, retries=8):
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
                msg = j['error'].get('message', '')
                if 'max usage' in msg.lower() or 'too many' in msg.lower() or 'rate' in msg.lower():
                    time.sleep(min(8, 0.5*(2**i))); continue
                return None
            logs = j['result']['meta'].get('logMessages', []) or []
            # Find the first `Program ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7 invoke [1]`
            # then the next `Program log: Instruction: XXX` is the top-level instr
            saw_exp = False
            for l in logs:
                if f'Program {EXPONENT} invoke' in l:
                    saw_exp = True
                    continue
                if saw_exp:
                    m = re.search(r'Instruction:\s*(\w+)', l)
                    if m:
                        return m.group(1)
                    if l.startswith('Program ') and 'success' in l:
                        saw_exp = False
            return None
        except Exception:
            time.sleep(min(8, 0.5*(2**i)))
    return None

def main():
    # Collect unique sigs from YT events
    sigs = set()
    with open(TRADES_IN) as f:
        for l in f:
            try: r = json.loads(l)
            except: continue
            if r.get('market'): sigs.add(r['sig'])
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
                json.dump(cache, open(CACHE, 'w'))
                rate = state['done']/max(time.time()-state['start'],1)
                eta = (len(pending)-state['done'])/max(rate,0.001)
                sys.stdout.write(f"\r{state['done']}/{len(pending)} ok={state['ok']} rate={rate:.1f}/s eta={eta/60:.1f}m")
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=25) as ex:
        futs = [ex.submit(fetch, s) for s in pending]
        for f in as_completed(futs): f.result()
    json.dump(cache, open(CACHE, 'w'))
    print(f"\nDone. cached={len(cache)}", flush=True)

    # Re-classify each event
    out_lines = []
    counts = {}
    with open(TRADES_IN) as f:
        for l in f:
            l = l.strip()
            if not l: continue
            try: r = json.loads(l)
            except: out_lines.append(l); continue
            if r.get('market'):
                inst = cache.get(r['sig'])
                r['instr'] = inst
                if inst in INSTR_TO_ACTION:
                    r['action'] = INSTR_TO_ACTION[inst]
                # else keep existing heuristic action
                counts[r['action']] = counts.get(r['action'], 0) + 1
            out_lines.append(json.dumps(r))
    with open(TRADES_IN, 'w') as f:
        f.write('\n'.join(out_lines) + '\n')
    print('Final event classification:')
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'  {k}: {v}')

if __name__ == '__main__':
    main()
