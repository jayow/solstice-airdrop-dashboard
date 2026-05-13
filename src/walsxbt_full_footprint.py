"""Complete Solstice footprint for @walsxbt (5Eentmk4) — every USX/eUSX-related
action, full timeline, current holdings, max position, days held, Kamino positions."""
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
CUTOFF = 1776038400  # 2026-04-13 00:00 UTC

PARTNERS = {
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'Kamino',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'Orca',
    '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': 'RaydiumAMM',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'RaydiumCLMM',
    'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7': 'Exponent',
    'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95': 'Solstice-claim',
    'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4': 'Jupiter',
}

session = requests.Session()
def rpc(m, p, retries=4):
    for i in range(retries):
        try:
            r = session.post(URL, json={'jsonrpc':'2.0','id':1,'method':m,'params':p}, timeout=30)
            if r.status_code in (429, 503): time.sleep(1); continue
            return r.json().get('result')
        except: time.sleep(1)
    return None

# Current holdings
print('=== CURRENT HOLDINGS ===')
for mint, label in [(USX, 'USX'), (EUSX, 'eUSX')]:
    accs = rpc('getTokenAccountsByOwner', [WALLET, {'mint': mint}, {'encoding': 'jsonParsed'}])
    bal = 0.0
    if accs and accs.get('value'):
        for acc in accs['value']:
            amt = float(acc['account']['data']['parsed']['info']['tokenAmount'].get('uiAmount') or 0)
            bal += amt
    print(f'  {label}: {bal:,.4f}')

# Kamino positions via getTokenAccountsByOwner — check Kamino ObligationFarm LP tokens too
print(f'\n=== ALL TOKEN HOLDINGS > $0 ===')
all_tokens = rpc('getTokenAccountsByOwner', [WALLET, {'programId': 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'}, {'encoding': 'jsonParsed'}])
holdings = []
if all_tokens and all_tokens.get('value'):
    for acc in all_tokens['value']:
        info = acc['account']['data']['parsed']['info']
        amt = float(info['tokenAmount'].get('uiAmount') or 0)
        if amt > 0.0001:
            holdings.append((info['mint'], amt))
holdings.sort(key=lambda x: -x[1])
for mint, amt in holdings[:20]:
    label = 'USX' if mint == USX else ('eUSX' if mint == EUSX else mint[:16]+'..')
    print(f'  {amt:>16,.4f}  {label}')

# Pull all sigs
print(f'\n=== FETCHING TX HISTORY ===')
sigs = []; before = None
while len(sigs) < 6000:
    p = [WALLET, {'limit': 1000}]
    if before: p[1]['before'] = before
    b = rpc('getSignaturesForAddress', p)
    if not b: break
    sigs.extend(b); before = b[-1]['signature']
    if len(b) < 1000: break
print(f'  {len(sigs)} total sigs')

def fetch(s): return s, rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])

records = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(fetch, s) for s in sigs]
    for i, f in enumerate(as_completed(futs)):
        if i % 500 == 0: print(f'  fetched {i}/{len(sigs)}', flush=True)
        s, tx = f.result()
        if tx: records.append((s, tx))

# Sort chronologically
records.sort(key=lambda x: x[0].get('blockTime') or 0)

# Build event timeline
events = []
usx_balance_tl = []   # (ts, running_balance) pairs
eusx_balance_tl = []
running = {'USX': 0.0, 'eUSX': 0.0}

for s, tx in records:
    ts = s.get('blockTime') or 0
    sig = s['signature']
    meta = tx.get('meta') or {}
    if meta.get('err'): continue
    msg = tx['transaction']['message']

    pre = {(b['accountIndex'], b.get('mint'), b.get('owner')): float(b['uiTokenAmount'].get('uiAmount') or 0) for b in (meta.get('preTokenBalances') or [])}
    post = {(b['accountIndex'], b.get('mint'), b.get('owner')): float(b['uiTokenAmount'].get('uiAmount') or 0) for b in (meta.get('postTokenBalances') or [])}

    usx_d = eusx_d = 0.0
    for k in set(pre) | set(post):
        idx, mint, owner = k
        if owner != WALLET: continue
        d = post.get(k, 0) - pre.get(k, 0)
        if abs(d) < 1e-9: continue
        if mint == USX: usx_d += d
        elif mint == EUSX: eusx_d += d

    if abs(usx_d) < 0.0001 and abs(eusx_d) < 0.0001: continue

    # Programs in tx
    programs = []
    for ix in msg.get('instructions', []):
        pid = ix.get('programId') or ix.get('program')
        if pid and pid in PARTNERS:
            programs.append(PARTNERS[pid])
    program_tag = ','.join(sorted(set(programs))) or 'transfer/other'

    running['USX'] += usx_d
    running['eUSX'] += eusx_d
    events.append({
        'ts': ts,
        'sig': sig,
        'usx_d': usx_d,
        'eusx_d': eusx_d,
        'usx_after': running['USX'],
        'eusx_after': running['eUSX'],
        'program': program_tag,
        'post_cutoff': ts >= CUTOFF,
    })

# Output timeline
print(f'\n{"="*100}')
print(f'FULL USX/eUSX TIMELINE FOR {WALLET}')
print(f'{"="*100}')
print(f'\n{"date":<20} {"USX Δ":>12} {"eUSX Δ":>12} {"USX bal":>12} {"eUSX bal":>12} {"program":<25} {"post-snap":<10}')
print('-'*110)
for e in events:
    d = dt.datetime.utcfromtimestamp(e['ts']).isoformat()[:16]
    post = 'POST-SNAP' if e['post_cutoff'] else ''
    print(f'  {d:<18} {e["usx_d"]:>+12,.2f} {e["eusx_d"]:>+12,.2f} {e["usx_after"]:>12,.2f} {e["eusx_after"]:>12,.2f}  {e["program"]:<25} {post}')

# Stats
if events:
    first = events[0]['ts']
    last = events[-1]['ts']
    max_usx = max(e['usx_after'] for e in events)
    max_eusx = max(e['eusx_after'] for e in events)

    # Days with non-zero balance (eUSX-days and USX-days)
    usx_days = eusx_days = 0.0
    prev_ts = events[0]['ts']
    prev_usx = prev_eusx = 0
    for e in events:
        dt_s = e['ts'] - prev_ts
        if prev_usx > 0.01: usx_days += prev_usx * dt_s / 86400.0  # balance-days
        if prev_eusx > 0.01: eusx_days += prev_eusx * dt_s / 86400.0
        prev_ts = e['ts']
        prev_usx = e['usx_after']
        prev_eusx = e['eusx_after']
    # Tail to now
    now = int(time.time())
    dt_s = now - prev_ts
    if prev_usx > 0.01: usx_days += prev_usx * dt_s / 86400.0
    if prev_eusx > 0.01: eusx_days += prev_eusx * dt_s / 86400.0

    # Simple "days any balance held"
    first_hold = None; last_hold = 0
    for e in events:
        if e['usx_after'] > 0.01 or e['eusx_after'] > 0.01:
            if first_hold is None: first_hold = e['ts']
            last_hold = e['ts']

    print(f'\n{"="*100}')
    print(f'SUMMARY')
    print(f'{"="*100}')
    print(f'  First USX/eUSX movement:  {dt.datetime.utcfromtimestamp(first).isoformat()} UTC')
    print(f'  Last USX/eUSX movement:   {dt.datetime.utcfromtimestamp(last).isoformat()} UTC')
    print(f'  Max USX balance ever:     {max_usx:,.2f}')
    print(f'  Max eUSX balance ever:    {max_eusx:,.2f}')
    print(f'  Current USX balance:      {running["USX"]:,.2f}')
    print(f'  Current eUSX balance:     {running["eUSX"]:,.2f}')
    if first_hold and last_hold:
        held_days = (last_hold - first_hold) / 86400
        print(f'  First → last hold date:   {(last_hold - first_hold) / 86400:.1f} days')
    print(f'  USX balance-days (integral): {usx_days:,.0f}  (ie avg balance × days)')
    print(f'  eUSX balance-days (integral): {eusx_days:,.0f}')

# Kamino specific — scan events for Kamino txs
print(f'\n=== KAMINO ACTIVITY ===')
kamino_events = [e for e in events if 'Kamino' in e['program']]
if kamino_events:
    for e in kamino_events:
        d = dt.datetime.utcfromtimestamp(e['ts']).isoformat()[:16]
        post = 'POST-SNAP' if e['post_cutoff'] else ''
        print(f'  {d}  USX {e["usx_d"]:+.2f}  eUSX {e["eusx_d"]:+.2f}  {post}  sig={e["sig"][:16]}..')

    supply_events = [e for e in kamino_events if e['usx_d'] < 0 or e['eusx_d'] < 0]
    withdraw_events = [e for e in kamino_events if e['usx_d'] > 0 or e['eusx_d'] > 0]
    total_supplied_usx = sum(-e['usx_d'] for e in supply_events if e['usx_d'] < 0)
    total_supplied_eusx = sum(-e['eusx_d'] for e in supply_events if e['eusx_d'] < 0)
    total_withdrawn_usx = sum(e['usx_d'] for e in withdraw_events if e['usx_d'] > 0)
    total_withdrawn_eusx = sum(e['eusx_d'] for e in withdraw_events if e['eusx_d'] > 0)
    print(f'\n  Total supplied:   USX {total_supplied_usx:,.2f}  eUSX {total_supplied_eusx:,.2f}')
    print(f'  Total withdrawn:  USX {total_withdrawn_usx:,.2f}  eUSX {total_withdrawn_eusx:,.2f}')
    if supply_events:
        first_supply = min(e['ts'] for e in supply_events)
        print(f'  First deposit: {dt.datetime.utcfromtimestamp(first_supply).isoformat()}')
    if withdraw_events:
        last_withdraw = max(e['ts'] for e in withdraw_events)
        print(f'  Last withdraw: {dt.datetime.utcfromtimestamp(last_withdraw).isoformat()}')
else:
    print('  No Kamino USX/eUSX events found')
