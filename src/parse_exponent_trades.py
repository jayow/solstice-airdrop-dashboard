#!/usr/bin/env python3
"""Parse Exponent YT buy/sell events via Helius Enhanced Transactions API."""
import os, json, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGS_IN = os.path.join(ROOT, 'data/exponent_sigs.json')
TXS_OUT = os.path.join(ROOT, 'data/exponent_trades.jsonl')
ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l=l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k,v=l.split('=',1); ENV[k]=v

RAW=ENV.get('HELIUS_API_KEY','').strip()
if RAW.startswith('http'):
    import re; m=re.search(r'api-key=([^&]+)', RAW); KEY=m.group(1) if m else ''
else: KEY=RAW
URL=f'https://api.helius.xyz/v0/transactions?api-key={KEY}&commitment=confirmed'

# Market configs mirror exponent_markets.js
MARKETS={
 'USX-09FEB26': dict(underlying='6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', ytMint='HQmMS5W34VcMtR85akhZgvypy7iqVWRXi282vwdf9eTX', ptMint='7vWj1UriSscGmz5wadAC8EkA8ndoU3M7WUifqxTC3Ysf', syMint='4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT', vault='9t936gEYkXJ5tFEMA6DnRVNwUTwNRRSQ87zocwou16gz', px=1.00),
 'USX-01JUN26': dict(underlying='6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', ytMint='Au8g11nXqXrUAmL14GM3gQnrnJnr4dcpgc5DNAnu9F9s', ptMint='3kctCXgt6pP3uZcek8SqNK2KZdQ6cqtj9hc3U46jhgBk', syMint='4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT', vault='DjDHnfWtVsAgNZJtLj8UWxBBYbBPC9xV69KHt7SzEXXy', px=1.00),
 'eUSX-11MAR26': dict(underlying='3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', ytMint='DDoYyEUcdkHV5a4NCPXDRL9f93NgPbqK9ZANAGL627wF', ptMint='6oiDcfve7ybKUC8ysZmncC9iSuxQG2vrRkh3dgV7EKR4', syMint='7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR', vault='8QshMo7i8RRKxPuU4kgbKVowCieKV1nf9H7Ycii2ZSXt', px=1.00),
 'eUSX-01JUN26': dict(underlying='3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', ytMint='GEYwnvNzqFXrLnNq4riXbn2ASnwU3cF8RXW6wXKHM4sw', ptMint='BNR2FsHo8JrYGWx2V8yxG5GBWiG3uU8voi2eMGBHFwEj', syMint='7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR', vault='BnqAo2Lpmg7BNP3mCUKBXRq5SFPqBLo6oDnqmsfUSpDG', px=1.00),
}
EXPONENT = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'

# SSL: use system trust store fallback
ctx = ssl.create_default_context()
try:
    import certifi; ctx = ssl.create_default_context(cafile=certifi.where())
except Exception: pass

def post(sigs, retries=12):
    body=json.dumps({'transactions': sigs}).encode()
    for i in range(retries):
        try:
            req=Request(URL, data=body, headers={'Content-Type':'application/json', 'User-Agent': 'curl/8.7.1'})
            r=urlopen(req, timeout=30, context=ctx)
            return json.loads(r.read())
        except HTTPError as e:
            if e.code in (413,429,403) or 'rate' in str(e).lower() or 'forbidden' in str(e).lower():
                time.sleep(min(5, 0.5*(2**i))); continue
            raise
        except (URLError, TimeoutError):
            time.sleep(min(5, 0.5*(2**i)))
    raise RuntimeError('retries exhausted')

def classify(tx):
    if not tx or tx.get('transactionError'): return []
    instrs = tx.get('instructions') or []
    inner = []
    for i in instrs:
        inner += i.get('innerInstructions') or []
    touched = any(ix.get('programId')==EXPONENT for ix in instrs+inner)
    if not touched: return []

    # Union of mints from tokenTransfers + accountData balance changes.
    transfers = tx.get('tokenTransfers') or []
    mints = {t.get('mint') for t in transfers}
    for ad in tx.get('accountData') or []:
        for bc in ad.get('tokenBalanceChanges') or []:
            m = bc.get('mint')
            if m: mints.add(m)

    # Every account mentioned anywhere in the tx — used to detect claim txs which
    # don't touch YT/PT mints but do touch the market vault address.
    accounts = set()
    for ad in tx.get('accountData') or []:
        a = ad.get('account')
        if a: accounts.add(a)
    for ix in (tx.get('instructions') or []):
        accounts.update(ix.get('accounts') or [])
        for iix in (ix.get('innerInstructions') or []):
            accounts.update(iix.get('accounts') or [])

    # Market-level identification by YT mint (preferred) → PT mint → market vault.
    hits = []
    for k, m in MARKETS.items():
        if (m['ytMint'] in mints
            or m['ptMint'] in mints
            or (m.get('vault') and m['vault'] in accounts)):
            hits.append((k, m))
    if not hits: return []

    signer = tx.get('feePayer')

    # Signer's net delta on a mint = sum of tokenTransfers +/- into them.
    def delta(mint):
        d = 0.0
        for t in transfers:
            if t.get('mint') != mint: continue
            if t.get('toUserAccount') == signer: d += float(t.get('tokenAmount') or 0)
            if t.get('fromUserAccount') == signer: d -= float(t.get('tokenAmount') or 0)
        return d

    out = []
    for k, m in hits:
        u  = delta(m['underlying'])
        sy = delta(m['syMint'])
        yt = delta(m['ytMint'])
        usdNet = (u + sy) * m['px']  # eUSX rate applied later by reprice_eusx.py
        action = 'other'
        if yt >  1e-4: action = 'buyYt'
        elif yt < -1e-4: action = 'sellYt'
        elif u  < -1e-4 or sy < -1e-4: action = 'buyYt'
        elif u  >  1e-4 or sy >  1e-4: action = 'sellYt'
        out.append({
            'sig': tx.get('signature'), 'blockTime': tx.get('timestamp'),
            'market': k, 'signer': signer, 'action': action,
            'ytDelta': round(yt, 6), 'underlyingDelta': round(u, 6),
            'syDelta': round(sy, 6), 'usdNet': round(usdNet, 4),
        })
    return out

def main():
    sigs=[s['signature'] for s in json.load(open(SIGS_IN))]
    # Resume
    done=set()
    if os.path.exists(TXS_OUT):
        with open(TXS_OUT) as f:
            kept=[]
            for l in f:
                l=l.strip()
                if not l: continue
                try: r=json.loads(l)
                except: continue
                if r.get('error'): continue
                done.add(r.get('sig'))
                kept.append(l)
        open(TXS_OUT,'w').write('\n'.join(kept)+'\n' if kept else '')
        print(f'Resuming: {len(done)} sigs already parsed', flush=True)
    queue=[s for s in sigs if s not in done]
    print(f'Total {len(sigs)}, to fetch {len(queue)}', flush=True)

    out=open(TXS_OUT,'a')
    lock=threading.Lock()
    state={'done':0,'total':len(queue)}
    def process(batch):
        txs=post(batch)
        lines=[]
        for i,sig in enumerate(batch):
            tx=txs[i] if i<len(txs) else None
            if not tx:
                lines.append(json.dumps({'sig':sig,'error':'no-tx'}))
                continue
            evs=classify(tx)
            if not evs:
                lines.append(json.dumps({'sig':sig,'blockTime':tx.get('timestamp'),'events':[]}))
            else:
                lines += [json.dumps(e) for e in evs]
        with lock:
            out.write('\n'.join(lines)+'\n')
            out.flush()
            state['done']+=len(batch)
            sys.stdout.write(f"\rparsed {state['done']}/{state['total']}")
            sys.stdout.flush()

    BATCH=100
    batches=[queue[i:i+BATCH] for i in range(0,len(queue),BATCH)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs=[ex.submit(process, b) for b in batches]
        for f in as_completed(futs):
            try: f.result()
            except Exception as e:
                with lock:
                    sys.stdout.write(f'\nbatch err: {e}\n'); sys.stdout.flush()
    out.close()
    print(f"\nDone. parsed={state['done']}")

if __name__=='__main__':
    main()
