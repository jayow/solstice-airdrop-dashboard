"""Decode 85F1bj5k claim contract — who calls it, who receives tokens, and how many are Solstice cohort members."""
import os, json, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'"); break

session = requests.Session()
def rpc(m, p, retries=6):
    for i in range(retries):
        try:
            r = session.post(URL, json={'jsonrpc':'2.0','id':1,'method':m,'params':p}, timeout=30)
            if r.status_code in (429, 503): time.sleep(min(8, 0.5*(2**i))); continue
            return r.json().get('result')
        except Exception as e:
            time.sleep(min(4, 0.5*(2**i)))
    return None

PROG = '85F1bj5k85LZxzHM35epKtHD5E11HcYsxLpV8VbyT6od'
SOLXTEST = 'Bt5azUhZG8VpxUttHLnCyE6bbPneqeQb4N4A2Ztes7rm'

sigs = []; before = None
while True:
    p = [PROG, {'limit': 1000}]
    if before: p[1]['before'] = before
    b = rpc('getSignaturesForAddress', p)
    if not b: break
    sigs.extend(b); before = b[-1]['signature']
    if len(b) < 1000: break
print(f'Total calls to 85F1bj5k: {len(sigs)}', flush=True)

regs = {}
for line in open(os.path.join(ROOT, 'data/solstice_registration/accounts.jsonl')):
    d = json.loads(line)
    if '_error' not in d: regs[d['walletAddress']]=d.get('cohort')
fp = {x['sender'] for x in json.load(open(os.path.join(ROOT, 'data/fee_payers.json')))}

signers = Counter()
claimed_amount = defaultdict(float)

def fetch(s): return s, rpc('getTransaction',[s['signature'],{'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])

with ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(fetch, s) for s in sigs]
    for i, f in enumerate(as_completed(futs)):
        if i % 200 == 0: print(f'  {i}/{len(sigs)}', flush=True)
        try:
            s, tx = f.result()
        except Exception:
            continue
        if not tx or (tx.get('meta') or {}).get('err'): continue
        msg = tx['transaction']['message']
        keys = msg.get('accountKeys', [])
        if not keys: continue
        signer = keys[0].get('pubkey') if isinstance(keys[0],dict) else keys[0]
        signers[signer] += 1
        meta = tx.get('meta') or {}
        pre = meta.get('preTokenBalances') or []
        post = meta.get('postTokenBalances') or []
        for b in post:
            if b.get('mint') != SOLXTEST: continue
            if b.get('owner') != signer: continue
            idx = b['accountIndex']
            pre_amt = next((float(x['uiTokenAmount'].get('uiAmount') or 0) for x in pre if x['accountIndex']==idx), 0)
            post_amt = float(b['uiTokenAmount'].get('uiAmount') or 0)
            d = post_amt - pre_amt
            if d > 0.0001:
                claimed_amount[signer] += d

print(f'\n=== RESULTS ===')
print(f'Total calls: {len(sigs):,}')
print(f'Unique signers: {len(signers):,}')
print(f'Claimants (received SOLXtest): {len(claimed_amount):,}')
print(f'Non-claiming callers: {len(signers) - len(claimed_amount):,}')
amts = sorted(claimed_amount.values(), reverse=True)
if amts:
    print(f'Claim size — max: {amts[0]:,.2f}  median: {amts[len(amts)//2]:,.2f}  min: {amts[-1]:,.2f}')
    print(f'Total SOLXtest distributed via this program: {sum(amts):,.2f}')

coh_claimants = {a: (claimed_amount[a], regs[a]) for a in claimed_amount if a in regs}
fp_claimants  = {a: claimed_amount[a] for a in claimed_amount if a in fp and a not in regs}
rand_claimants= {a: claimed_amount[a] for a in claimed_amount if a not in fp and a not in regs}
print(f'\nClaimants in Solstice cohort: {len(coh_claimants)}')
for a,(amt,c) in sorted(coh_claimants.items(), key=lambda x: -x[1][0]):
    print(f'  cohort={c}  amt={amt:>10,.2f}  {a}')
print(f'\nClaimants who are Solstice fee-payers (no cohort): {len(fp_claimants)}')
print(f'Claimants NOT in Solstice dataset: {len(rand_claimants)}')
print(f'  total claimed by non-Solstice wallets: {sum(rand_claimants.values()):,.2f}')

# Save
out = os.path.join(ROOT, 'data/85F1bj5k_decoded.json')
with open(out, 'w') as f:
    json.dump({
        'program': PROG,
        'total_calls': len(sigs),
        'unique_signers': len(signers),
        'claimants': len(claimed_amount),
        'cohort_claimants': {k: {'amount': v[0], 'cohort': v[1]} for k,v in coh_claimants.items()},
        'fp_claimants': fp_claimants,
        'random_claimants_count': len(rand_claimants),
        'random_claimants_total_claimed': sum(rand_claimants.values()),
    }, f, indent=2, default=str)
print(f'\nwrote {out}')
