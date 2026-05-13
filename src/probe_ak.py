"""Probe AKjfJDv4yw — the funder of FA1qn's first Solstice fee payment."""
import json, os, time, datetime as dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

session = requests.Session()

def rpc(m, p, retries=4):
    for i in range(retries):
        try:
            r = session.post(URL, json={'jsonrpc':'2.0','id':1,'method':m,'params':p}, timeout=30)
            if r.status_code in (429, 503): time.sleep(1); continue
            return r.json().get('result')
        except Exception as e:
            time.sleep(1)
    return None

AK = 'AKjfJDv4ywdpCDrj7AURuNkGA3696GTVFgrMwk4TjkKs'

sigs = []
before = None
MAX = 5000
while len(sigs) < MAX:
    p = [AK, {'limit': 1000}]
    if before: p[1]['before'] = before
    b = rpc('getSignaturesForAddress', p)
    if not b: break
    sigs.extend(b)
    before = b[-1]['signature']
    print(f'  fetched {len(sigs)} sigs', flush=True)
    if len(b) < 1000: break

bal = rpc('getBalance', [AK])
print(f'\nAKjfJDv4yw: {len(sigs)} sigs fetched (cap={MAX}), balance {(bal.get("value",0) if bal else 0)/1e9:.4f} SOL', flush=True)

if len(sigs) >= MAX:
    print(f'WARNING: hit sig cap. Oldest: {dt.datetime.utcfromtimestamp(sigs[-1].get("blockTime") or 0).isoformat()}')
    print(f'Newest: {dt.datetime.utcfromtimestamp(sigs[0].get("blockTime") or 0).isoformat()}')

recipients = Counter(); rec_tot = defaultdict(float)
senders = Counter(); snd_tot = defaultdict(float)
events = []

def fetch(s):
    return s, rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])

with ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(fetch, s) for s in sigs]
    for i, f in enumerate(as_completed(futs)):
        if i % 500 == 0: print(f'  {i}/{len(sigs)}', flush=True)
        s, tx = f.result()
        if not tx or (tx.get('meta') or {}).get('err'): continue
        meta = tx['meta']; msg = tx['transaction']['message']
        keys = [(k.get('pubkey') if isinstance(k, dict) else k) for k in msg.get('accountKeys', [])]
        if AK not in keys: continue
        idx = keys.index(AK)
        pre = meta.get('preBalances') or []; post = meta.get('postBalances') or []
        if idx >= len(pre): continue
        delta = (post[idx] - pre[idx]) / 1e9
        if idx == 0: delta += (meta.get('fee') or 0) / 1e9
        if abs(delta) < 0.0001: continue
        best = None; mag = 0
        for i2, k in enumerate(keys):
            if k == AK: continue
            if i2 >= len(pre): continue
            d = (post[i2] - pre[i2]) / 1e9
            if i2 == 0: d += (meta.get('fee') or 0) / 1e9
            if delta > 0 and d < -0.0001 and abs(d) > mag: mag = abs(d); best = k
            elif delta < 0 and d > 0.0001 and abs(d) > mag: mag = abs(d); best = k
        events.append((s.get('blockTime') or 0, delta, best, s['signature']))
        if best:
            if delta < 0: recipients[best] += 1; rec_tot[best] += abs(delta)
            else: senders[best] += 1; snd_tot[best] += delta

regs = {}
for line in open(os.path.join(ROOT, 'data/solstice_registration/accounts.jsonl')):
    d = json.loads(line)
    if '_error' in d: continue
    regs[d['walletAddress']] = d.get('cohort')

print(f'\nSent to {len(recipients)} distinct addresses:')
for addr, n in recipients.most_common(25):
    tag = f'  [cohort={regs[addr]}]' if addr in regs else ''
    print(f'  {rec_tot[addr]:>9.4f} SOL ({n}x) -> {addr}{tag}')

print(f'\nReceived from {len(senders)} distinct addresses:')
for addr, n in senders.most_common(25):
    tag = f'  [cohort={regs[addr]}]' if addr in regs else ''
    print(f'  {snd_tot[addr]:>9.4f} SOL ({n}x) <- {addr}{tag}')

print(f'\nTimeline ({len(events)} events):')
for ts, d, cp, sig in sorted(events):
    dstr = dt.datetime.utcfromtimestamp(ts).isoformat() if ts else '?'
    tag = f' [c={regs[cp]}]' if cp in regs else ''
    print(f'  {dstr}  {d:+9.4f}  {cp}{tag}')
