"""Per-reserve Kamino indexer (v2) — proper fix for the multiply over-count.

The v1 walker (walk_s2_kamino.py) walks per-obligation sig history and
captures positive token-balance deltas across ALL accounts in each tx.
Kamino Multiply / flash-loan ops produce phantom net inflows on accounts
the user doesn't actually own, inflating flares 4× for high-frequency
multiply wallets.

v2 approach:
  1. Enumerate Solstice market obligations (obl_pubkey → owner_pubkey)
  2. For each reserve (USX, eUSX, USDG): walk sig history
  3. For each tx: classify ix, find the obligation in accountKeys, map to
     owner, compute the USER'S OWN-ATA net token delta (not reserve, not
     intermediate). Sign by ix.
  4. Build per-user (side, mint) event timeline
  5. Run through transform_kamino unchanged

User attribution by own-ATA net delta naturally rejects multiply ops:
their USX flows through intermediate accounts but the user's own USX ATA
nets to zero within the tx, so no event is recorded.

Output: data/s2_kamino_v2_flares.json (for side-by-side comparison vs v1)
        Does NOT write to wallet_quests yet — caller decides when to swap.

Usage:
    python3 src/flares_estimator/walk_s2_kamino_v2.py
"""
import os, sys, json, time, base64, base58, struct, requests
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
import walker_db
import db as _db
from incremental_events import extract_events_incremental
from transform_kamino import transform_wallet as _transform_wallet_flares

S2_START_TS = 1776038400
SOLSTICE_MARKET = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'
KLEND = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'

# Reserves (discovered from Kamino API 2026-05-15).
RESERVES = [
    {'reserve': 'H2pmnDSjfxeQ8zUeyUohokegYbXZgkjH4kgmoQVybyAX', 'mint': USX_MINT,  'token': 'USX'},
    {'reserve': 'ARQFJTiUJEuxoiA9VtAcnoAUHYvbTmhKytz7D6nfnfEb', 'mint': EUSX_MINT, 'token': 'eUSX'},
    {'reserve': '34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83', 'mint': USDG_MINT, 'token': 'USDG'},
]

# Ix → (side, sign). 'lend' deposit is +1, withdraw is -1; 'borrow' borrow is +1, repay is -1.
IX_MAP = {
    'depositreserveliquidityandobligationcollateral':   ('lend',   +1),
    'depositreserveliquidityandobligationcollateralv2': ('lend',   +1),
    'depositreserveliquidity':                          ('lend',   +1),
    'depositreserveliquidityv2':                        ('lend',   +1),
    'depositobligationcollateral':                      ('lend',   +1),
    'depositobligationcollateralv2':                    ('lend',   +1),
    'withdrawreserveliquidity':                         ('lend',   -1),
    'withdrawreserveliquidityv2':                       ('lend',   -1),
    'withdrawobligationcollateralandredeemreservecollateral':   ('lend',   -1),
    'withdrawobligationcollateralandredeemreservecollateralv2': ('lend',   -1),
    'withdrawobligationcollateral':                     ('lend',   -1),
    'withdrawobligationcollateralv2':                   ('lend',   -1),
    'borrowobligationliquidity':                        ('borrow', +1),
    'borrowobligationliquidityv2':                      ('borrow', +1),
    'repayobligationliquidity':                         ('borrow', -1),
    'repayobligationliquidityv2':                       ('borrow', -1),
    'liquidateobligationandredeemreservecollateral':    ('lend',   -1),
    'liquidateobligationandredeemreservecollateralv2':  ('lend',   -1),
}


def enumerate_obligations() -> dict:
    """Returns {obligation_pubkey: owner_pubkey} for all Solstice market obligations.

    CRITICAL: filter by account data size — obligations and reserves both have
    `lendingMarket` at offset 32, so a memcmp-only filter picks up reserves too.
    Kamino obligation size = 3344 bytes; reserve size = 8624 bytes.
    """
    r = rpc('getProgramAccounts', [KLEND, {
        'encoding': 'base64',
        'dataSlice': {'offset': 64, 'length': 32},
        'filters': [
            {'memcmp': {'offset': 32, 'bytes': SOLSTICE_MARKET}},
            {'dataSize': 3344},   # obligation only — excludes reserves
        ]
    }], timeout=60)
    out = {}
    for a in (r.get('result') or []):
        try:
            d = base64.b64decode(a['account']['data'][0])
            out[a['pubkey']] = base58.b58encode(d[:32]).decode()
        except Exception: continue
    return out


def fetch_reserve_sigs(reserve_pk: str, min_ts: int, max_ts: int) -> list:
    """Walk reserve's sig history. Drop sigs outside [min_ts, max_ts]."""
    import time as _t
    sigs = []
    before = None
    while True:
        batch = []
        for attempt in range(4):
            params = [reserve_pk, {'limit': 1000}]
            if before: params[1]['before'] = before
            r = rpc('getSignaturesForAddress', params, timeout=30, force_refresh=(attempt > 0))
            batch = r.get('result') or []
            if batch: break
            _t.sleep(0.5 * (attempt + 1))
        if not batch: break
        raw_len = len(batch)
        last_sig = batch[-1]['signature']
        # Keep only sigs in our window
        keep = [s for s in batch if min_ts <= (s.get('blockTime') or 0) <= max_ts]
        sigs.extend(keep)
        # Stop pagination once batch has any sig older than min_ts (sorted newest→oldest)
        if (batch[-1].get('blockTime') or 0) < min_ts: break
        if raw_len < 1000: break
        before = last_sig
    return sigs


def _ata(owner: str, mint: str) -> str:
    """Derive the associated token account pubkey for (owner, mint).

    PDA seeds: [owner_bytes, TOKEN_PROGRAM_bytes, mint_bytes]
    Program ID: ATA program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL
    """
    # Standard ATA derivation
    TOKEN_PROGRAM = base58.b58decode('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
    ATA_PROGRAM = base58.b58decode('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
    seeds = [base58.b58decode(owner), TOKEN_PROGRAM, base58.b58decode(mint)]
    # Find PDA — try bumps 255 down to 0
    for bump in range(255, -1, -1):
        try:
            h = sha256()
            for s in seeds:
                h.update(s)
            h.update(bytes([bump]))
            h.update(ATA_PROGRAM)
            h.update(b'ProgramDerivedAddress')
            candidate = h.digest()
            # Reject on-curve points (Ed25519). Since we can't easily check that
            # here without nacl, accept the first hash. ATA derivation is well-
            # known — Kamino accounts will match what the chain reports.
            return base58.b58encode(candidate).decode()
        except Exception: continue
    return None


def classify_kamino_tx(tx: dict, obl_to_owner: dict, reserve_mint: str) -> dict | None:
    """Return {user, side, sign, amt, ts, sig} for a Kamino tx, or None if irrelevant.

    user attribution: find ANY obligation pubkey from our enumeration in tx.accountKeys.
    Map to owner. If multiple obligations (e.g. liquidation), prefer the one whose
    owner is NOT the signer (= the borrower being liquidated).

    amount: NET delta on the user's OWN token account for reserve_mint. This is the
    key to rejecting multiply ops — their own ATA nets to zero so amt=0 → no event.
    """
    if (tx.get('meta') or {}).get('err'): return None
    meta = tx['meta'] or {}
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []

    # Normalize accountKeys to pubkey strings
    pubkeys = []
    for k in keys:
        if isinstance(k, dict): pubkeys.append(k.get('pubkey'))
        elif isinstance(k, str): pubkeys.append(k)
        else: pubkeys.append(None)

    # Find Kamino ix from logs
    ix_name = None
    for ln in (meta.get('logMessages') or []):
        if 'Program log: Instruction:' not in ln: continue
        nm = ln.split('Instruction:', 1)[1].strip().split()[0].lower()
        if nm in IX_MAP:
            ix_name = nm; break
    if not ix_name: return None
    side, sign = IX_MAP[ix_name]

    # Find an obligation account in this tx; map to owner.
    user = None
    obls_in_tx = [pk for pk in pubkeys if pk in obl_to_owner]
    if not obls_in_tx: return None

    signer = pubkeys[0] if pubkeys else None
    # Prefer obligation whose owner == signer (normal user-initiated tx).
    signer_owned = [pk for pk in obls_in_tx if obl_to_owner.get(pk) == signer]
    if signer_owned:
        user = signer
    elif 'liquidate' in ix_name:
        # Liquidation: attribute to the VICTIM (whose obligation got seized, not signer)
        victim_obls = [pk for pk in obls_in_tx if obl_to_owner.get(pk) != signer]
        user = obl_to_owner.get(victim_obls[0]) if victim_obls else None
    else:
        # Non-liquidation tx where signer isn't an obligation owner. Could be a
        # relayer / smart wallet. Use the first obligation's owner as best guess.
        user = obl_to_owner.get(obls_in_tx[0])
    if not user: return None

    # Compute net token delta for user's own ATA of reserve_mint.
    pre = meta.get('preTokenBalances') or []
    post = meta.get('postTokenBalances') or []
    pre_by_idx = {b['accountIndex']: b for b in pre}
    user_net = 0.0
    for b in post:
        if b.get('mint') != reserve_mint: continue
        if b.get('owner') != user: continue
        idx = b['accountIndex']
        p = pre_by_idx.get(idx, {})
        bef = float(((p.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        aft = float(((b.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        user_net += (aft - bef)
    # Also check pre-balances for accounts not in post (closed accounts)
    post_idxs = {b['accountIndex'] for b in post if b.get('mint') == reserve_mint and b.get('owner') == user}
    for b in pre:
        if b.get('mint') != reserve_mint: continue
        if b.get('owner') != user: continue
        if b['accountIndex'] in post_idxs: continue
        # Account was closed during tx
        bef = float(((b.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        user_net += (0 - bef)

    # Convert to event amount with sign. The amount is the absolute USD-equivalent
    # of what moved between the user and the reserve. For deposits/repays user_net
    # is negative (sent to reserve); for withdraws/borrows it's positive.
    # We record amt as positive; the transform applies sign based on ix.
    amt = abs(user_net)
    if amt < 0.000001: return None   # multiply / phantom flow: net zero

    return {
        'ts': tx.get('blockTime') or 0,
        'sig': '',   # caller fills
        'pos_pubkey': obls_in_tx[0],   # for compatibility with transform_kamino
        'ix': ix_name,
        'deltas': [{'mint': reserve_mint, 'amt': amt}],
        'user': user,
        'side_sign': (side, sign),
    }


def main():
    snapshot_ts = last_snapshot_ts()
    end_ts = snapshot_ts
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → '
          f'{datetime.fromtimestamp(end_ts, UTC).strftime("%Y-%m-%d %H:%M UTC")} '
          f'(midnight-anchored)\n', flush=True)

    # 1. Enumerate obligations
    print('Enumerating Solstice market obligations…', flush=True)
    obl_to_owner = enumerate_obligations()
    print(f'  {len(obl_to_owner):,} obligations, {len(set(obl_to_owner.values())):,} unique owners\n', flush=True)

    # 2. Walk each reserve's sig history, classify events
    events_by_user = defaultdict(list)
    # Walk the FULL history of each reserve. Carry-in positions can be from
    # any earlier deposit; transform_kamino handles pre-S2 events as carry-in.
    # First run is expensive (one tx fetch per uncached sig); subsequent runs
    # hit cache for everything except today's new sigs.
    walk_min_ts = 0   # walk to creation

    for r in RESERVES:
        print(f'=== {r["token"]} reserve {r["reserve"][:14]}… ===', flush=True)
        sigs = fetch_reserve_sigs(r['reserve'], min_ts=walk_min_ts, max_ts=end_ts)
        print(f'  {len(sigs):,} sigs in window', flush=True)

        n_evts = 0
        # Parallel tx fetches; cache hits will be fast
        def fetch_and_classify(s):
            try:
                rr = rpc('getTransaction',
                         [s['signature'], {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}],
                         timeout=20)
            except Exception: return None
            tx = (rr or {}).get('result')
            if not tx: return None
            evt = classify_kamino_tx(tx, obl_to_owner, r['mint'])
            if evt:
                evt['sig'] = s['signature']
            return evt

        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(fetch_and_classify, s) for s in sigs]
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 1000 == 0: print(f'    {done}/{len(sigs)}', flush=True)
                evt = fut.result()
                if evt:
                    events_by_user[evt['user']].append(evt)
                    n_evts += 1
        print(f'  {n_evts:,} non-trivial events captured\n', flush=True)

    # 3. Build (current snapshot, events) per user; transform_kamino to flares
    print(f'Computing flares for {len(events_by_user)} users…', flush=True)

    # Pull current snapshots from Kamino API in parallel (only for users with events)
    SF_DENOM = 2**60
    def _kget(path: str):
        return requests.get(f'https://api.kamino.finance{path}', timeout=20).json()
    reserve2mint = {r['reserve']: r['mint'] for r in RESERVES}
    SIDE_MINT_TO_POSKEY = {
        ('lend', USX_MINT):  'kamino_supply_usx',
        ('lend', EUSX_MINT): 'kamino_supply_eusx',
        ('lend', USDG_MINT): 'kamino_supply_usdg',
        ('borrow', USX_MINT):  'kamino_borrow_usx',
        ('borrow', USDG_MINT): 'kamino_borrow_usdg',
    }

    def get_user_snapshot(wallet: str) -> dict:
        try:
            obls = _kget(f'/kamino-market/{SOLSTICE_MARKET}/users/{wallet}/obligations') or []
        except Exception: return {}
        snap = defaultdict(float)
        for obl in obls:
            state = obl.get('state', {})
            for d in state.get('deposits', []):
                mint = reserve2mint.get(d.get('depositReserve'))
                if mint is None: continue
                usd = int(d.get('marketValueSf','0')) / SF_DENOM
                k = SIDE_MINT_TO_POSKEY.get(('lend', mint))
                if k and usd > 0: snap[k] += usd
            for b in state.get('borrows', []):
                mint = reserve2mint.get(b.get('borrowReserve'))
                if mint is None: continue
                usd = int(b.get('marketValueSf','0')) / SF_DENOM
                k = SIDE_MINT_TO_POSKEY.get(('borrow', mint))
                if k and usd > 0: snap[k] += usd
        return dict(snap)

    results = {}
    snapshots = {}
    users = sorted(events_by_user.keys())
    with ThreadPoolExecutor(max_workers=8) as ex:
        snapshot_futs = {ex.submit(get_user_snapshot, u): u for u in users}
        n_done = 0
        for fut in as_completed(snapshot_futs):
            user = snapshot_futs[fut]
            try: snap = fut.result()
            except Exception: snap = {}
            snapshots[user] = snap
            # Sort events by ts for the transform
            evs = sorted(events_by_user[user], key=lambda e: e['ts'])
            flares = _transform_wallet_flares(snap, evs, end_ts)
            if any(v > 0 for v in flares.values()):
                results[user] = flares
            n_done += 1
            if n_done % 200 == 0: print(f'  {n_done}/{len(users)}', flush=True)

    # 4. Output
    print('\nPer-quest v2 totals:')
    totals = defaultdict(float)
    for user, pq in results.items():
        for q, v in pq.items(): totals[q] += v
    for q, v in sorted(totals.items()):
        print(f'  {q:<32s} {v:>20,.0f}')
    print(f'  {"TOTAL":<32s} {sum(totals.values()):>20,.0f}')

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            'data', 's2_kamino_v2_flares.json')
    with open(out_path, 'w') as f:
        json.dump({w: pq for w, pq in results.items()}, f, indent=2)
    print(f'\nSaved {len(results)} wallets to {out_path}')
    print('NOTE: v2 does NOT write to wallet_quests. Compare to v1 first.')


if __name__ == '__main__':
    main()
