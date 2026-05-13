"""Deep analysis of 5Eentmk4 (@walsxbt) — verify if his claims are true:
  1. "early to farming Flares" - check first USX/eUSX activity timestamp
  2. "5-fig+ position size" - reconstruct max USX/eUSX balance over time
  3. "did Flares quests to LP" - any LP activity on partner pools?
  4. "empty txs in dashboard despite activity" - count real trades vs our record

Only looks at PRE-SNAPSHOT activity (before 2026-04-13 UTC).
"""
import os, json, time, datetime as dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'"); break

WALLET = '5Eentmk4CCCX5w8a81aWujHbUpQ1GrHFX8zbcJb1rxnN'
USX = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
SOLSTICE_MINTS = {USX, EUSX}
CUTOFF = 1776038400  # 2026-04-13 00:00 UTC

# Known partner programs
PARTNERS = {
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'Kamino',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'Orca',
    '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': 'Raydium AMM',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'Raydium CLMM',
    'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7': 'Exponent',
}

session = requests.Session()
def rpc(m, p, retries=4):
    for i in range(retries):
        try:
            r = session.post(URL, json={'jsonrpc':'2.0','id':1,'method':m,'params':p}, timeout=30)
            if r.status_code in (429, 503): time.sleep(1); continue
            return r.json().get('result')
        except Exception: time.sleep(1)
    return None

# Fetch all sigs
sigs = []; before = None
while len(sigs) < 5000:
    p = [WALLET, {'limit': 1000}]
    if before: p[1]['before'] = before
    b = rpc('getSignaturesForAddress', p)
    if not b: break
    sigs.extend(b); before = b[-1]['signature']
    print(f'  sigs: {len(sigs)}  oldest: {dt.datetime.utcfromtimestamp(b[-1].get("blockTime") or 0).isoformat()[:10]}', flush=True)
    if len(b) < 1000: break

pre_snap = [s for s in sigs if (s.get('blockTime') or 0) < CUTOFF]
print(f'\n{len(sigs):,} total sigs, {len(pre_snap):,} pre-snapshot')

# Fetch all txs
def fetch(s): return s, rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])

records = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(fetch, s) for s in pre_snap]
    for i, f in enumerate(as_completed(futs)):
        if i % 200 == 0: print(f'  fetched {i}/{len(pre_snap)}', flush=True)
        s, tx = f.result()
        if tx: records.append((s, tx))

# Analyze
events = []  # list of (ts, mint, delta, program_label, signer_was_wallet, sig)
partner_activity = defaultdict(list)  # partner_name -> list of (ts, sig, is_signer, usx_delta, eusx_delta)
max_balance_by_mint = {}
running_balance_by_mint = defaultdict(float)
first_usx_activity = None
first_eusx_activity = None
solstice_tx_count = 0
non_solstice_tx_count = 0

all_progs_cnt = Counter()
solstice_progs_cnt = Counter()

for s, tx in sorted(records, key=lambda x: x[0].get('blockTime') or 0):
    ts = s.get('blockTime') or 0
    sig = s['signature']
    meta = tx.get('meta') or {}
    if meta.get('err'): continue
    msg = tx['transaction']['message']
    keys = msg.get('accountKeys', [])
    signer = keys[0].get('pubkey') if isinstance(keys[0], dict) else keys[0]

    # Check if tx touches USX/eUSX in any way
    pre_bal = {(b['accountIndex'], b.get('mint'), b.get('owner')): float(b['uiTokenAmount'].get('uiAmount') or 0) for b in (meta.get('preTokenBalances') or [])}
    post_bal = {(b['accountIndex'], b.get('mint'), b.get('owner')): float(b['uiTokenAmount'].get('uiAmount') or 0) for b in (meta.get('postTokenBalances') or [])}
    all_k = set(pre_bal) | set(post_bal)

    mints_touched = {k[1] for k in all_k if k[1]}
    is_solstice_tx = bool(mints_touched & SOLSTICE_MINTS)
    if is_solstice_tx:
        solstice_tx_count += 1
    else:
        non_solstice_tx_count += 1

    # Programs invoked (top-level)
    programs_in_tx = set()
    for ix in msg.get('instructions', []):
        pid = ix.get('programId') or ix.get('program')
        if pid:
            all_progs_cnt[pid] += 1
            programs_in_tx.add(pid)
            if is_solstice_tx:
                solstice_progs_cnt[pid] += 1

    # Wallet's own USX/eUSX ATA deltas
    usx_d = 0.0; eusx_d = 0.0
    for k in all_k:
        idx, mint, owner = k
        if owner != WALLET: continue
        d = post_bal.get(k, 0) - pre_bal.get(k, 0)
        if abs(d) < 1e-9: continue
        if mint == USX:
            usx_d += d
            if first_usx_activity is None: first_usx_activity = ts
        elif mint == EUSX:
            eusx_d += d
            if first_eusx_activity is None: first_eusx_activity = ts

    # Track running balance + max
    if usx_d:
        running_balance_by_mint['USX'] += usx_d
        max_balance_by_mint['USX'] = max(max_balance_by_mint.get('USX', 0), running_balance_by_mint['USX'])
    if eusx_d:
        running_balance_by_mint['eUSX'] += eusx_d
        max_balance_by_mint['eUSX'] = max(max_balance_by_mint.get('eUSX', 0), running_balance_by_mint['eUSX'])

    # Partner activity
    for pid, label in PARTNERS.items():
        if pid in programs_in_tx and is_solstice_tx:
            partner_activity[label].append({
                'ts': ts,
                'sig': sig,
                'signer_is_wallet': signer == WALLET,
                'usx_delta_on_wallet_ata': usx_d,
                'eusx_delta_on_wallet_ata': eusx_d,
                'mints_in_tx': sorted(mints_touched),
            })

# Report
print(f'\n{"="*80}')
print(f'STUDY RESULTS — {WALLET}')
print(f'{"="*80}')

print(f'\n[1] EARLY TO FLARES?')
if first_usx_activity:
    print(f'    First USX activity:  {dt.datetime.utcfromtimestamp(first_usx_activity).isoformat()} UTC')
if first_eusx_activity:
    print(f'    First eUSX activity: {dt.datetime.utcfromtimestamp(first_eusx_activity).isoformat()} UTC')
if not first_usx_activity and not first_eusx_activity:
    print(f'    Never held USX or eUSX on own ATAs')

print(f'\n[2] 5-FIG+ POSITION SIZE?')
print(f'    Max USX balance (on wallet ATA, reconstructed): {max_balance_by_mint.get("USX", 0):,.2f}')
print(f'    Max eUSX balance: {max_balance_by_mint.get("eUSX", 0):,.2f}')
print(f'    Current USX running: {running_balance_by_mint.get("USX", 0):,.2f}')
print(f'    Current eUSX running: {running_balance_by_mint.get("eUSX", 0):,.2f}')
print(f'    (Note: reconstructed from per-tx deltas; may differ slightly from real-time balances)')

print(f'\n[3] DID FLARES QUESTS TO LP?')
print(f'    USX/eUSX-touching txs: {solstice_tx_count}')
print(f'    Non-USX/eUSX txs:      {non_solstice_tx_count}')
print(f'\n    Partner-program txs involving USX/eUSX:')
for partner, events_list in partner_activity.items():
    signer_count = sum(1 for e in events_list if e['signer_is_wallet'])
    with_delta = sum(1 for e in events_list if abs(e['usx_delta_on_wallet_ata']) > 0.01 or abs(e['eusx_delta_on_wallet_ata']) > 0.01)
    print(f'      {partner:<15} {len(events_list):>4} txs  (wallet signed: {signer_count}, with real ATA delta: {with_delta})')

print(f'\n[4] DASHBOARD ACCURACY')
print(f'    Total wallet txs pre-snapshot: {len(records):,}')
print(f'    Of those, touching USX/eUSX: {solstice_tx_count} ({100*solstice_tx_count/max(len(records),1):.1f}%)')
print(f'    Meaning {100*non_solstice_tx_count/max(len(records),1):.1f}% of activity is NON-Solstice (Jupiter/Phoenix/Meteora trading unrelated to USX)')

print(f'\n[5] TOP PROGRAMS IN USX/eUSX-RELATED TXS ONLY:')
known = {
    '11111111111111111111111111111111': 'System', 'ComputeBudget111111111111111111111111111111': 'ComputeBudget',
    'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA': 'SPL Token', 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb': 'Token-2022',
    'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL': 'ATA', 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4': 'Jupiter V6',
    'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95': 'Loopscale (unknown)',
    'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7': 'Exponent',
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'Kamino',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'Orca Whirlpool',
    '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': 'Raydium AMM',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'Raydium CLMM',
    'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA': 'pAMM (Phoenix?)',
    'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo': 'Meteora DLMM',
}
for pid, n in solstice_progs_cnt.most_common(15):
    if pid in ('11111111111111111111111111111111','ComputeBudget111111111111111111111111111111',
               'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
               'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'):
        continue
    name = known.get(pid, pid[:20]+'..')
    print(f'    {n:>4}x  {name}')

# Save
out = os.path.join(ROOT, 'data/walsxbt_study.json')
with open(out, 'w') as f:
    json.dump({
        'wallet': WALLET,
        'totalTxs': len(sigs),
        'preSnapshotTxs': len(pre_snap),
        'firstUsxActivity': first_usx_activity,
        'firstEusxActivity': first_eusx_activity,
        'maxUsx': max_balance_by_mint.get('USX', 0),
        'maxEusx': max_balance_by_mint.get('eUSX', 0),
        'solsticeTxs': solstice_tx_count,
        'nonSolsticeTxs': non_solstice_tx_count,
        'partnerActivity': {k: v for k, v in partner_activity.items()},
    }, f, indent=2, default=str)
print(f'\nwrote {out}')
