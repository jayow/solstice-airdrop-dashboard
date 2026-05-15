"""Enrich S2_LOOPSCALE cache with per-position event history.

Walks both:
  - Loan accounts (disc 14c34675a5e3b601, principal_mint=USX at off=92)
    → borrow side, borrower at offset 11
  - VaultStake accounts (disc e1228035a7efb66b, vault=USX-ONE at off=8)
    → supply side, user at offset 73

For each, walks all sigs (incl. pre-S2 opens) on the position PDA, classifies
each tx by Loopscale Anchor ix name from program logs, and emits an event
with token deltas (USX/eUSX/USDG/USDC sums).

Writes per-user snapshot + events to quest_cache['S2_LOOPSCALE'] so the drawer
shows position cards with event history like Orca/Raydium/Kamino.
"""
import os, sys, time, base58, base64, struct, json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db
from incremental_events import extract_events_incremental

S2_START_TS = 1776038400
LOOPSCALE_PROG   = '1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78'
LOAN_DISC        = '14c34675a5e3b601'
VAULTSTAKE_DISC  = 'e1228035a7efb66b'
USX_ONE_VAULT    = '3s3vAaYpwkyjrgzpBRwgSDxpwHPD1jic25mb1VDzM8Rk'
USX_MINT         = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT        = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
PRINCIPAL_SCALE  = 1e9

LOOP_IXS = {
    'borrow_principal', 'borrowprincipal',
    'repay_principal',  'repayprincipal',
    'create_loan',      'createloan',
    'close_loan',       'closeloan',
    'lock_loan',        'lockloan',
    'deposit_collateral','depositcollateral',
    'withdraw_collateral','withdrawcollateral',
    'deposit_principal','depositprincipal',
    'withdraw_principal','withdrawprincipal',
    'stake', 'unstake',
    'liquidate_loan',   'liquidateloan',
    'roll_loan',        'rollloan',
    'collect_fees',     'collectfees',
}

RELEVANT_MINTS = {USX_MINT, EUSX_MINT,
                  '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',  # USDG
                  'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',  # USDC
                  '3PQotuGMnMgEXrErizQbzPPhSMb79xQgkEDn2hk2KPWn'}  # Loopscale USX-ONE LP


def _classify_loopscale_tx(tx: dict, s: dict) -> dict | None:
    """Classify a single Loopscale tx → event dict, or None to skip."""
    ix_name = None
    for ln in (tx['meta'].get('logMessages') or []):
        if 'Program log: Instruction:' in ln:
            nm = ln.split('Instruction:', 1)[1].strip().split()[0].lower()
            if nm in LOOP_IXS:
                ix_name = nm; break
    if not ix_name: return None
    pre = tx['meta'].get('preTokenBalances') or []
    post = tx['meta'].get('postTokenBalances') or []
    pre_by_idx = {b['accountIndex']: b for b in pre}
    deltas = {}
    for b in post:
        if b.get('mint') not in RELEVANT_MINTS: continue
        p = pre_by_idx.get(b['accountIndex'], {})
        bef = float(((p.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        aft = float(((b.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        d = aft - bef
        if d > 0:
            deltas[b['mint']] = deltas.get(b['mint'], 0) + d
    return {
        'ix': ix_name,
        'deltas': [{'mint': m, 'amt': a} for m, a in deltas.items()],
    }


def _extract_events_for_position(pos_pubkey: str, existing_for_pos: list) -> list:
    """Incremental walk — return only events newer than the last cached one
    for this position. Caller merges with `existing_for_pos`."""
    return extract_events_incremental(pos_pubkey, existing_for_pos, _classify_loopscale_tx, walker_name='walk_s2_loopscale_events')


def main():
    db.init()
    now_ts = int(time.time())

    print('=== Enumerating Loopscale Loan accounts (USX principal) ===')
    filters = [
        {'memcmp': {'offset': 0,  'bytes': base58.b58encode(bytes.fromhex(LOAN_DISC)).decode()}},
        {'memcmp': {'offset': 92, 'bytes': USX_MINT}},
    ]
    r = rpc('getProgramAccounts', [LOOPSCALE_PROG, {'encoding':'base64', 'filters': filters}], timeout=180)
    loans = r.get('result') or []
    print(f'  {len(loans)} USX Loan accounts')

    print('\n=== Enumerating VaultStake accounts (USX-ONE vault) ===')
    filters = [
        {'memcmp': {'offset': 0, 'bytes': base58.b58encode(bytes.fromhex(VAULTSTAKE_DISC)).decode()}},
        {'memcmp': {'offset': 8, 'bytes': USX_ONE_VAULT}},
    ]
    r = rpc('getProgramAccounts', [LOOPSCALE_PROG, {'encoding':'base64', 'filters': filters}], timeout=120)
    stakes = r.get('result') or []
    print(f'  {len(stakes)} VaultStake accounts')

    # Decode owners and snapshot principal values
    # Loan: borrower=off 11, principal_remaining=off 155 (units 1e9), status=off 10
    # VaultStake: user=off 73
    loan_records = []   # (pos_pk, borrower, principal_usx)
    stake_records = []  # (pos_pk, user)
    for a in loans:
        d = base64.b64decode(a['account']['data'][0])
        if len(d) < 200: continue
        status = d[10]   # 0 = active
        borrower = base58.b58encode(d[11:43]).decode()
        principal = struct.unpack_from('<Q', d, 155)[0] / PRINCIPAL_SCALE
        loan_records.append((a['pubkey'], borrower, principal, status))
    for a in stakes:
        d = base64.b64decode(a['account']['data'][0])
        if len(d) < 105: continue
        user = base58.b58encode(d[73:105]).decode()
        stake_records.append((a['pubkey'], user))

    print(f'\n=== Walking signatures + extracting events (incremental) ===')
    events_by_user = defaultdict(list)
    snap_by_user = defaultdict(lambda: defaultdict(float))

    # Preload existing cached events per (wallet, pos_pubkey) so the
    # incremental walk knows what we've already processed.
    existing_by_user_pos = defaultdict(list)   # (wallet, pos_pk) -> [events]
    for r in db.conn().execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_LOOPSCALE'"):
        try:
            for e in (json.loads(r['raw_json']).get('events') or []):
                pp = e.get('pos_pubkey')
                if pp: existing_by_user_pos[(r['wallet'], pp)].append(e)
        except Exception: pass

    def walk_loan(rec):
        pos_pk, borrower, principal, status = rec
        existing = existing_by_user_pos.get((borrower, pos_pk), [])
        new_evs = _extract_events_for_position(pos_pk, existing)
        for e in new_evs: e['side'] = 'borrow'
        return borrower, principal, status, existing, new_evs

    def walk_stake(rec):
        pos_pk, user = rec
        existing = existing_by_user_pos.get((user, pos_pk), [])
        new_evs = _extract_events_for_position(pos_pk, existing)
        for e in new_evs: e['side'] = 'supply'
        return user, existing, new_evs

    new_event_count = 0
    # Process loans
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(walk_loan, r) for r in loan_records]
        done = 0
        for fut in as_completed(futs):
            borrower, principal, status, existing, new_evs = fut.result()
            done += 1
            if done % 25 == 0: print(f'  loans: {done}/{len(loan_records)}', flush=True)
            if status == 0 and principal > 0:
                snap_by_user[borrower]['loopscale_borrow_usx'] += principal
            # Merge existing + new (no duplicates because of `until_sig` boundary)
            events_by_user[borrower].extend(existing)
            events_by_user[borrower].extend(new_evs)
            new_event_count += len(new_evs)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(walk_stake, r) for r in stake_records]
        done = 0
        for fut in as_completed(futs):
            user, existing, new_evs = fut.result()
            done += 1
            if done % 25 == 0: print(f'  stakes: {done}/{len(stake_records)}', flush=True)
            events_by_user[user].extend(existing)
            events_by_user[user].extend(new_evs)
            new_event_count += len(new_evs)
    print(f'\n  +{new_event_count} new events captured this run')

    # Merge in existing snapshot values for `loopscale_supply_usx` from
    # the current S2_LOOPSCALE cache (so we don't clobber gt_loopscale_supply's value).
    existing_supply = {}
    for r in db.conn().execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_LOOPSCALE'"):
        try:
            raw = json.loads(r['raw_json'])
            v = (raw.get('positions') or {}).get('loopscale_supply_usx')
            if v: existing_supply[r['wallet']] = v
        except Exception: pass

    print(f'\nWriting per-user snapshots + events to quest_cache')
    snap_count = 0
    all_users = set(snap_by_user.keys()) | set(events_by_user.keys()) | set(existing_supply.keys())
    for u in all_users:
        evs = events_by_user.get(u, [])
        evs.sort(key=lambda e: e.get('ts') or 0)
        snap = {
            'positions': {
                'loopscale_supply_usx': round(existing_supply.get(u, 0), 2),
                'loopscale_borrow_usx': round(snap_by_user[u].get('loopscale_borrow_usx', 0), 2),
            },
            'events': evs,
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(u, 'S2_LOOPSCALE', snap, watermark_ts=now_ts)
        snap_count += 1
    print(f'Per-wallet snapshots written: {snap_count}  ({sum(len(v) for v in events_by_user.values())} total events)')


if __name__ == '__main__':
    main()
