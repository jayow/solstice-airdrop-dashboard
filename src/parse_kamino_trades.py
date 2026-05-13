#!/usr/bin/env python3
"""Parse each Kamino signature into per-user lending events.

For each tx on any Solstice-market reserve, emit one event per (signer, reserve_symbol):
  {sig, blockTime, reserve, mint, signer, action, underlyingDelta, usdNet}

Action is derived from the user's token balance delta of the reserve's liquidity mint:
  • positive delta  → user received the asset  → 'withdraw' (supply redemption) OR 'borrow'
  • negative delta  → user sent the asset      → 'supply' OR 'repay'

We disambiguate using the raw Kamino instruction name from logs (supplied later by
classify_kamino_events.py) — for now we emit a best-effort 'other' placeholder and
let the log-reader classifier assign the exact action.

Output: data/kamino_events.jsonl (one JSON per line).
Resumable — skips sigs already processed, retries errors.
"""
import os, json, re, time, threading, sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from kamino_markets import RESERVES, RESERVE_TO_SYM, MINT_TO_RESERVE, KAMINO_LEND_PROGRAM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGS_IN = os.path.join(ROOT, 'data/kamino_sigs.json')
OUT     = os.path.join(ROOT, 'data/kamino_events.jsonl')

ENV = {}
for l in open(os.path.join(ROOT, '.env')):
    l = l.strip()
    if not l or l.startswith('#') or '=' not in l: continue
    k, v = l.split('=', 1); ENV[k] = v
RAW = ENV['HELIUS_API_KEY']
KEY = re.search(r'api-key=([^&]+)', RAW).group(1) if RAW.startswith('http') else RAW
URL = f'https://api.helius.xyz/v0/transactions?api-key={KEY}&commitment=confirmed'

BATCH = 100
CONCURRENCY = 6

def fetch_batch(sigs, retries=12):
    for i in range(retries):
        try:
            r = requests.post(URL, json={'transactions': sigs},
                              headers={'Content-Type':'application/json','User-Agent':'curl/8.7.1'}, timeout=45)
            if r.status_code in (429, 413, 403, 503, 504):
                time.sleep(min(8, 0.5*(2**i))); continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            time.sleep(min(8, 0.5*(2**i)))
    raise RuntimeError('retries exhausted')

def classify_from_deltas(tx, signer):
    """Return list of events for signer across reserves touched in this tx."""
    if not tx or tx.get('transactionError'): return []
    transfers = tx.get('tokenTransfers') or []
    # Check Kamino program was invoked
    instrs = tx.get('instructions') or []
    touched = any((i.get('programId') == KAMINO_LEND_PROGRAM) for i in instrs)
    if not touched: return []

    # Identify which reserve mints are involved
    reserves_hit = []
    for tr in transfers:
        m = tr.get('mint')
        if m in MINT_TO_RESERVE:
            reserves_hit.append(m)
    if not reserves_hit: return []

    # Signer's net delta per mint
    events = []
    for mint in set(reserves_hit):
        sym, r = MINT_TO_RESERVE[mint]
        d = 0.0
        for tr in transfers:
            if tr.get('mint') != mint: continue
            if tr.get('toUserAccount') == signer:   d += float(tr.get('tokenAmount') or 0)
            if tr.get('fromUserAccount') == signer: d -= float(tr.get('tokenAmount') or 0)
        if abs(d) < 1e-9: continue
        # Placeholder action — the log-reader classifier will set the exact kind
        action = 'inflow' if d > 0 else 'outflow'
        usd = abs(d) * r['px']
        events.append({
            'sig': tx.get('signature'), 'blockTime': tx.get('timestamp'),
            'reserve': sym, 'mint': mint, 'signer': signer, 'action': action,
            'underlyingDelta': round(d, 6), 'usdNet': round(usd, 4),
        })
    return events

def process_batch(batch, out, state, lock):
    try:
        txs = fetch_batch(batch)
    except Exception as e:
        with lock:
            for sig in batch: out.write(json.dumps({'sig': sig, 'error': str(e)}) + '\n')
            state['done'] += len(batch)
            sys.stdout.write(f"\rparsed {state['done']}/{state['total']} (batch err: {e})")
            sys.stdout.flush()
        return
    lines = []
    for i, sig in enumerate(batch):
        tx = txs[i] if i < len(txs) else None
        if not tx:
            lines.append(json.dumps({'sig': sig, 'error': 'no-tx'})); continue
        signer = tx.get('feePayer')
        evs = classify_from_deltas(tx, signer)
        if not evs:
            lines.append(json.dumps({'sig': sig, 'blockTime': tx.get('timestamp'), 'events': []}))
        else:
            for ev in evs: lines.append(json.dumps(ev))
    with lock:
        out.write('\n'.join(lines) + '\n'); out.flush()
        state['done'] += len(batch)
        if state['done'] % 500 == 0 or state['done'] == state['total']:
            rate = state['done'] / max(time.time() - state['start'], 1)
            eta = (state['total'] - state['done']) / max(rate, 0.001)
            sys.stdout.write(f"\rparsed {state['done']}/{state['total']}  rate={rate:.1f}/s eta={eta/60:.1f}m")
            sys.stdout.flush()

def main():
    sigs = [s['signature'] for s in json.load(open(SIGS_IN))]
    print(f'Loaded {len(sigs)} Kamino sigs', flush=True)

    # Resume: drop errors; keep successful entries
    done = set()
    if os.path.exists(OUT):
        kept = []
        for l in open(OUT):
            l = l.strip()
            if not l: continue
            try: r = json.loads(l)
            except: continue
            if r.get('error'): continue
            done.add(r.get('sig'))
            kept.append(l)
        open(OUT,'w').write('\n'.join(kept)+'\n' if kept else '')
        print(f'Resuming: {len(done)} sigs already parsed', flush=True)

    queue = [s for s in sigs if s not in done]
    batches = [queue[i:i+BATCH] for i in range(0, len(queue), BATCH)]
    print(f'Fetching {len(queue)} txs in {len(batches)} batches × {CONCURRENCY} workers...', flush=True)

    out = open(OUT, 'a')
    lock = threading.Lock()
    state = {'done': 0, 'total': len(queue), 'start': time.time()}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(process_batch, b, out, state, lock) for b in batches]
        for f in as_completed(futs): f.result()
    out.close()
    print(f"\nDone. parsed={state['done']}")

if __name__ == '__main__':
    main()
