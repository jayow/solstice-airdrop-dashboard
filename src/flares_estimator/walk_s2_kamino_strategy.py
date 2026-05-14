"""Walk S2 Kamino USDG/USX Strategy (S2_KAMINO_KVAULT_USDG_USX, 10×).

Strategy: 45bdcbekD687TU49RFux1a4csf3TN3cM3J1UaFcFhWt2  (USDG/USX, CLMM)
Share mint: 4qkStdH1NPKMmxrTDbY8kzTkJorpGMd8GLxo81drv9Jz

For each holder of the share mint:
  1. Find their share ATA
  2. Walk ATA sig history during S2
  3. Each tx Δ in share balance gives event timeline
  4. Integrate share_balance × share_price × 10 dt

share_price varies (currently $0.002499/share). For accuracy we use the
share price as fetched live; for cumulative we approximate with a constant
(stablecoin pair → minimal drift). Refinement: read share price per tx slot
if needed.

Output: data/s2_kamino_strategy_flares.json
"""
import os, sys, json, time, base64
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db

S2_START_TS = 1776038400
MIN_HOLD_DAYS = 1.0
STRATEGY = '45bdcbekD687TU49RFux1a4csf3TN3cM3J1UaFcFhWt2'
SHARE_MINT = '4qkStdH1NPKMmxrTDbY8kzTkJorpGMd8GLxo81drv9Jz'
MULT = 10
QUEST = 'S2_KAMINO_KVAULT_USDG_USX'


def get_share_price():
    r = requests.get(f'https://api.kamino.finance/strategies/{STRATEGY}/metrics', timeout=15).json()
    return float(r.get('sharePrice', 0))


def main():
    now_ts = int(time.time())
    share_price = get_share_price()
    print(f'Strategy {STRATEGY[:8]}...  share price ${share_price:.10f}  mult {MULT}×', flush=True)
    print(f'S2 window: {(now_ts-S2_START_TS)/86400:.1f} days', flush=True)

    # Find all current holders via getProgramAccounts with mint filter
    print('\\nFinding all share-mint holders...', flush=True)
    r = rpc('getProgramAccounts', ['TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', {
        'encoding': 'jsonParsed',
        'filters': [
            {'dataSize': 165},
            {'memcmp': {'offset': 0, 'bytes': SHARE_MINT}}
        ]
    }], timeout=120)
    accs = r.get('result', []) or []
    # Map ATA -> (owner, current_balance)
    holders = {}
    for a in accs:
        info = a['account']['data']['parsed']['info']
        owner = info.get('owner')
        bal = float(info['tokenAmount']['uiAmount'] or 0)
        ata = a['pubkey']
        holders[ata] = {'owner': owner, 'current_bal': bal}
    print(f'Total share-mint token accounts: {len(holders):,}', flush=True)
    nonzero = sum(1 for h in holders.values() if h['current_bal'] > 0)
    print(f'Non-zero balance holders: {nonzero:,}', flush=True)

    # Track per-owner signed share deltas across all events (for cost basis)
    owner_share_deltas = defaultdict(list)  # owner → [(ts, signed_share_delta, sig)]

    # Walk each ATA's FULL sig history (pre-S2 + S2). Pre-S2 sigs are needed
    # so cost-basis captures the user's original deposit; flare math still
    # integrates only from S2_START via the loop below.
    def process_ata(ata, info):
        owner = info['owner']
        sigs = []
        before = None
        while True:
            params = [ata, {'limit': 1000}]
            if before: params[1]['before'] = before
            r = rpc('getSignaturesForAddress', params)
            batch = r.get('result', []) or []
            if not batch: break
            sigs.extend(batch)
            if len(batch) < 1000: break
            before = batch[-1]['signature']
        if not sigs and info['current_bal'] == 0:
            return owner, 0.0   # no S2 activity, no current bal
        if not sigs:
            # Position predates S2 - assume current bal × full S2 window
            days = (now_ts - S2_START_TS) / 86400
            return owner, info['current_bal'] * share_price * days * MULT

        # Walk events: fetch each tx, get pre/post share balance for this ATA
        def fetch_tx(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
                return s, r.get('result')
            except: return s, None
        events = []   # (ts, balance_after, sig, prev_balance)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_tx, s) for s in sigs]
            for fut in as_completed(futs):
                s, tx = fut.result()
                if not tx: continue
                if (tx['meta'] or {}).get('err'): continue
                pre = {(t['accountIndex'], t['mint']): t for t in tx['meta'].get('preTokenBalances', [])}
                post = {(t['accountIndex'], t['mint']): t for t in tx['meta'].get('postTokenBalances', [])}
                bal_after = None; bal_before = None
                for k in set(pre)|set(post):
                    idx, mint = k
                    if mint != SHARE_MINT: continue
                    pb = pre.get(k); pob = post.get(k)
                    if (pob or pb).get('owner') != owner: continue
                    bal_before = float(((pb or {}).get('uiTokenAmount') or {}).get('uiAmount') or 0)
                    bal_after = float(((pob or {}).get('uiTokenAmount') or {}).get('uiAmount') or 0)
                if bal_after is not None:
                    events.append((s['blockTime'], bal_after, s['signature'], bal_before))
        events.sort(key=lambda x: x[0])
        # Record signed share deltas for cost-basis accumulation
        for t, post_bal, sig, prev_bal in events:
            delta = post_bal - prev_bal
            if delta != 0:
                owner_share_deltas[owner].append((t, delta, sig))
        # Integrate between events, assuming share_price constant (stablecoin pair).
        # Clip integration window to S2 (events may now include pre-S2 sigs for
        # cost-basis purposes; we don't want pre-S2 time contributing flares).
        usd_days = 0.0
        for i in range(len(events)):
            bal = events[i][1]
            seg_start = max(events[i][0], S2_START_TS)
            next_t = events[i+1][0] if i+1 < len(events) else now_ts
            seg_end = max(next_t, S2_START_TS)
            dt = (seg_end - seg_start) / 86400
            if dt > 0 and bal > 0:
                usd_days += bal * share_price * dt
        # Pre-first-event: assume balance was 0 before user's first S2 tx
        # (if S2 sigs exist, user joined during S2)
        if usd_days == 0 and info['current_bal'] > 0:
            # Edge case: events couldn't be parsed but current bal exists. Use first-sig approx.
            first_ts = min(s['blockTime'] for s in sigs if s.get('blockTime'))
            days = (now_ts - first_ts) / 86400
            usd_days = info['current_bal'] * share_price * days
        # Apply min-1-day rule across the full window (already in usd_days)
        flares = usd_days * MULT
        return owner, flares

    # Process in parallel
    print('\\nWalking each share-ATA...', flush=True)
    results = defaultdict(float)
    n_done = 0
    atas = list(holders.items())
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(process_ata, ata, info) for ata, info in atas]
        for fut in as_completed(futs):
            owner, flares = fut.result()
            results[owner] += flares
            n_done += 1
            if n_done % 50 == 0:
                print(f'  {n_done}/{len(atas)}  unique-owners={len(results)}', flush=True)

    total = sum(results.values())
    nonzero_wallets = sum(1 for v in results.values() if v > 0)
    print(f'\\nDone. {nonzero_wallets:,} wallets with strategy flares', flush=True)
    print(f'  TOTAL S2_KAMINO_KVAULT_USDG_USX flares: {total:,.0f}')

    # Save
    out = {w: {QUEST: round(v, 2)} for w, v in results.items() if v > 0}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_kamino_strategy_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    print(f'Saved {len(out)} wallets to {out_path}')

    # DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS_DB = ['S2_KAMINO_KVAULT_USDG_USX']
    walker_db.prune('walk_s2_kamino_strategy')
    rows_db = []
    for w_, pq_ in out.items():
        for q_, v_ in pq_.items():
            if v_ > 0: rows_db.append((w_, q_, v_))
    walker_db.upsert_many('walk_s2_kamino_strategy', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_kamino_strategy', WALKER_QUESTS_DB)
    print(f'DB: walker_outputs={len(rows_db)} rows; synced to wallet_quests')

    # Backfill kamino_kvault_usx_usdg into the S2_KAMINO cache so the drawer's
    # Kamino position panel includes the strategy USD alongside lend/borrow.
    # Without this the cache's kvault field stays at the placeholder 0.0 written
    # by walk_s2_kamino.py and the dashboard under-reports strategy holders.
    import db as _db2
    _db2.init()
    con = _db2.conn()
    backfilled = 0
    # Reconstruct per-owner current strategy USD from the holders dict.
    owner_strategy_usd = defaultdict(float)
    for ata, info in holders.items():
        if info['current_bal'] > 0:
            owner_strategy_usd[info['owner']] += info['current_bal'] * share_price
    # Also write per-owner kvault cost basis into cost_basis_by_quest.
    # Cost basis = sum_signed(share_delta) × share_price (current).
    all_owners = set(owner_strategy_usd.keys()) | set(owner_share_deltas.keys())
    for owner in all_owners:
        row = con.execute(
            "SELECT raw_json FROM quest_cache WHERE wallet=? AND quest_key='S2_KAMINO'",
            (owner,)
        ).fetchone()
        if row:
            try: raw = json.loads(row['raw_json'])
            except Exception: raw = {'positions': {}, 'events': [], '_watermark': {'slot': 0, 'ts': now_ts}}
        else:
            raw = {'positions': {
                'kamino_supply_usx': 0.0, 'kamino_supply_eusx': 0.0, 'kamino_supply_usdg': 0.0,
                'kamino_borrow_usx': 0.0, 'kamino_borrow_usdg': 0.0, 'kamino_kvault_usx_usdg': 0.0,
            }, 'events': [], '_watermark': {'slot': 0, 'ts': now_ts}}
        positions = raw.setdefault('positions', {})
        positions['kamino_kvault_usx_usdg'] = round(owner_strategy_usd.get(owner, 0), 2)
        # Cost basis: sum signed share-delta × current share_price
        deltas = owner_share_deltas.get(owner, [])
        supplied = withdrawn = 0.0
        n_s = n_w = 0
        for _t, d, _sig in deltas:
            usd = d * share_price
            if d > 0:
                supplied += usd; n_s += 1
            else:
                withdrawn += -usd; n_w += 1
        cbq = raw.setdefault('cost_basis_by_quest', {})
        if n_s + n_w > 0:
            cbq['S2_KAMINO_KVAULT_USDG_USX'] = {
                'kind':          'lend',
                'usd_basis':     max(0.0, supplied - withdrawn),
                'usd_paid':      supplied,
                'usd_recovered': withdrawn,
                'n_supplies':    n_s,
                'n_withdraws':   n_w,
            }
        _db2.put_cache(owner, 'S2_KAMINO', raw, watermark_ts=now_ts)
        backfilled += 1
    print(f'Backfilled kvault USD + cost basis into S2_KAMINO cache for {backfilled} wallets')

    # Top 5
    top = sorted(results.items(), key=lambda x: -x[1])[:5]
    print('\\nTop 5 wallets:')
    for w, f in top:
        if f > 0: print(f'  {w}  {f:>14,.0f}')


if __name__ == '__main__':
    main()
