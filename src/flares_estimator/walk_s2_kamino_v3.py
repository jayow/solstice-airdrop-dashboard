"""Per-reserve Kamino indexer v3 — ix-data amount parsing.

Fixes the v2 over-rejection bug (multiply borrows mis-rejected) by reading the
AUTHORITATIVE amount from the Kamino instruction's data bytes, not from the
user's wallet token-balance delta.

v2 used user's own-ATA net delta. This correctly excluded multiply ops where
wallet flows cancel, but ALSO excluded legitimate multiply BORROWS where the
obligation borrowed for real (flash loan masks the wallet view).

v3 reads ix.data[8:16] as the amount. For Kamino:
  - deposit/borrow/repay: amount is in liquidity (underlying) tokens
  - withdraw: amount is in cTokens (collateral). For LEND_USX/EUSX/USDG quests
    we treat cTokens 1:1 with underlying (the exchange-rate drift on a
    stablecoin reserve over S2 is < 1%).

Ix type identified by 8-byte discriminator (computed as sha256("global:<snake_name>")[:8]).
Reserve target identified by which Solstice reserve pubkey appears in ix.accounts.
User identified by obligation pubkey in ix.accounts → owner map.

Output: data/s2_kamino_v3_flares.json. Does NOT write to wallet_quests.
"""
import os, sys, json, time, base64, base58, struct, requests
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
import walker_db
import db as _db
from transform_kamino import transform_wallet as _transform_wallet_flares

S2_START_TS = 1776038400
SOLSTICE_MARKET = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'
KLEND = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'

# Solstice reserves on Kamino
RESERVES = [
    {'reserve': 'H2pmnDSjfxeQ8zUeyUohokegYbXZgkjH4kgmoQVybyAX', 'mint': USX_MINT,  'token': 'USX'},
    {'reserve': 'ARQFJTiUJEuxoiA9VtAcnoAUHYvbTmhKytz7D6nfnfEb', 'mint': EUSX_MINT, 'token': 'eUSX'},
    {'reserve': '34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83', 'mint': USDG_MINT, 'token': 'USDG'},
]

# Kamino ix discriminators (anchor: sha256("global:<snake_name>")[:8]).
# Maps hex-encoded 8-byte disc → (ix_name, side, sign).
# Side: 'lend' = supply/withdraw to/from reserve liquidity.
#       'borrow' = borrow/repay against reserve.
# Sign: +1 increases user's position, -1 decreases.
DISC_MAP = {
    'a9c91e7e06cd6644': ('deposit_reserve_liquidity',                                       'lend',   +1),
    '81c70402de271a2e': ('deposit_reserve_liquidity_and_obligation_collateral',             'lend',   +1),
    'd8e0bf1bcc9766af': ('deposit_reserve_liquidity_and_obligation_collateral_v2',          'lend',   +1),
    '00174d97e0646770': ('withdraw_reserve_liquidity',                                       'lend',   -1),
    '4b5d5ddc2296dac4': ('withdraw_obligation_collateral_and_redeem_reserve_collateral',     'lend',   -1),
    'eb34779895c51407': ('withdraw_obligation_collateral_and_redeem_reserve_collateral_v2',  'lend',   -1),
    '797f12cc49f5e141': ('borrow_obligation_liquidity',                                      'borrow', +1),
    'a1808ff5abc7c206': ('borrow_obligation_liquidity_v2',                                   'borrow', +1),
    '91b20de14cf09348': ('repay_obligation_liquidity',                                       'borrow', -1),
    '74aed54cb435d290': ('repay_obligation_liquidity_v2',                                    'borrow', -1),
    'b1479abce2854a37': ('liquidate_obligation_and_redeem_reserve_collateral',               'lend',   -1),
    'a2a1238f1ebbb967': ('liquidate_obligation_and_redeem_reserve_collateral_v2',            'lend',   -1),
    # deposit_obligation_collateral / withdraw_obligation_collateral don't touch reserve
    # liquidity directly — they re-arrange collateral between user wallet and obligation.
    # Skipping for now.
}


def enumerate_obligations() -> dict:
    """Returns {obligation_pubkey: owner_pubkey} for Solstice market obligations.
    Filters by dataSize=3344 to exclude reserves that share lendingMarket offset."""
    r = rpc('getProgramAccounts', [KLEND, {
        'encoding': 'base64',
        'dataSlice': {'offset': 64, 'length': 32},
        'filters': [
            {'memcmp': {'offset': 32, 'bytes': SOLSTICE_MARKET}},
            {'dataSize': 3344},
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
    """Walk reserve's sig history newest→oldest, keeping sigs in [min_ts, max_ts]."""
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
        keep = [s for s in batch if min_ts <= (s.get('blockTime') or 0) <= max_ts]
        sigs.extend(keep)
        if (batch[-1].get('blockTime') or 0) < min_ts: break
        if raw_len < 1000: break
        before = last_sig
    return sigs


def _ix_accounts_pubkeys(ix: dict) -> list:
    """Normalize ix.accounts to a list of pubkey strings (works for outer + inner)."""
    accts = ix.get('accounts') or []
    out = []
    for a in accts:
        if isinstance(a, dict):
            out.append(a.get('pubkey'))
        elif isinstance(a, str):
            out.append(a)
    return out


def _iter_kamino_ixs(tx: dict):
    """Yield (ix_dict, source_str) for every Kamino-program ix in the tx,
    walking both outer and inner instructions."""
    msg = (tx.get('transaction') or {}).get('message') or {}
    for ix in (msg.get('instructions') or []):
        pid = ix.get('programId') or ix.get('program')
        if pid == KLEND:
            yield ix, 'outer'
    for grp in ((tx.get('meta') or {}).get('innerInstructions') or []):
        for ix in (grp.get('instructions') or []):
            pid = ix.get('programId') or ix.get('program')
            if pid == KLEND:
                yield ix, 'inner'


def classify_kamino_tx_v3(tx: dict, obl_to_owner: dict, reserve_pk: str, reserve_mint: str):
    """Return list of events for this tx targeting `reserve_pk`. Each event:
      {ts, sig, ix, side, sign, amount_raw, owner, pos_pubkey}

    A single tx can contain MULTIPLE Kamino ixs against the same reserve (e.g.
    multiply: deposit + borrow), so we yield all of them.
    """
    if (tx.get('meta') or {}).get('err'): return []
    sig = (tx.get('transaction') or {}).get('signatures', [None])[0]
    ts = tx.get('blockTime') or 0
    events = []

    for ix, src in _iter_kamino_ixs(tx):
        data_b58 = ix.get('data') or ''
        try:
            data = base58.b58decode(data_b58)
        except Exception: continue
        if len(data) < 16: continue   # no amount field — refresh or admin ix
        disc = data[:8].hex()
        if disc not in DISC_MAP: continue   # not one of our tracked ix types
        ix_name, side, sign = DISC_MAP[disc]
        amount_raw = int.from_bytes(data[8:16], 'little')
        if amount_raw == 0: continue
        # Special-case: u64::MAX (-1 as i64) means "withdraw/repay all". Walker
        # treats it as 0 for now (unknown exact amount); transform's carry_in
        # math compensates from current snapshot.
        if amount_raw == 0xFFFFFFFFFFFFFFFF: continue

        accts = _ix_accounts_pubkeys(ix)
        # Filter to ixs targeting OUR reserve
        if reserve_pk not in accts: continue

        # Find user via obligation pubkey in accounts
        obls_in_ix = [pk for pk in accts if pk in obl_to_owner]
        if not obls_in_ix: continue
        # Multiple obligations → liquidation (signer is liquidator, victim is the obl owner)
        # For now, attribute to first obligation (most common case: user is signer + their obl)
        owner = obl_to_owner[obls_in_ix[0]]

        # Treat amount_raw as raw token units; convert to UI by /10^6 (all 3 tracked mints have 6 decimals)
        amount_ui = amount_raw / 1e6

        # Normalize ix name to transform_kamino's IX_MAP format (lowercase, no underscores)
        ix_norm = ix_name.replace('_', '')
        events.append({
            'ts': ts,
            'sig': sig,
            'ix': ix_norm,
            'pos_pubkey': obls_in_ix[0],
            'deltas': [{'mint': reserve_mint, 'amt': amount_ui}],
            'owner': owner,
            'side_sign': (side, sign),
            'src': src,
            'disc': disc,
        })
    return events


def main():
    snapshot_ts = last_snapshot_ts()
    end_ts = snapshot_ts
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → '
          f'{datetime.fromtimestamp(end_ts, UTC).strftime("%Y-%m-%d %H:%M UTC")}', flush=True)

    print('Enumerating obligations…', flush=True)
    obl_to_owner = enumerate_obligations()
    print(f'  {len(obl_to_owner):,} obligations, {len(set(obl_to_owner.values())):,} unique owners\n', flush=True)

    events_by_user = defaultdict(list)
    walk_min_ts = 0   # walk to reserve creation

    for r in RESERVES:
        print(f'=== {r["token"]} reserve {r["reserve"][:14]}… ===', flush=True)
        sigs = fetch_reserve_sigs(r['reserve'], min_ts=walk_min_ts, max_ts=end_ts)
        print(f'  {len(sigs):,} sigs in window', flush=True)

        n_evts = 0
        def fetch_and_classify(s):
            try:
                rr = rpc('getTransaction', [s['signature'],
                         {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}],
                        timeout=20)
            except Exception: return []
            tx = (rr or {}).get('result')
            if not tx: return []
            return classify_kamino_tx_v3(tx, obl_to_owner, r['reserve'], r['mint'])

        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(fetch_and_classify, s) for s in sigs]
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 2000 == 0: print(f'    {done}/{len(sigs)}', flush=True)
                for evt in fut.result():
                    events_by_user[evt['owner']].append(evt)
                    n_evts += 1
        print(f'  {n_evts:,} events captured\n', flush=True)

    # Transform per user: snapshot from Kamino API + events from on-chain
    print(f'Computing flares for {len(events_by_user)} users…', flush=True)
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
    snapshots = {}   # keep around for cache-write step below
    users = sorted(events_by_user.keys())
    with ThreadPoolExecutor(max_workers=8) as ex:
        snapshot_futs = {ex.submit(get_user_snapshot, u): u for u in users}
        n_done = 0
        for fut in as_completed(snapshot_futs):
            user = snapshot_futs[fut]
            try: snap = fut.result()
            except Exception: snap = {}
            snapshots[user] = snap
            evs = sorted(events_by_user[user], key=lambda e: e['ts'])
            flares = _transform_wallet_flares(snap, evs, end_ts)
            if any(v > 0 for v in flares.values()):
                results[user] = flares
            n_done += 1
            if n_done % 200 == 0: print(f'  {n_done}/{len(users)}', flush=True)

    print('\nPer-quest v3 totals:')
    totals = defaultdict(float)
    for user, pq in results.items():
        for q, v in pq.items(): totals[q] += v
    for q, v in sorted(totals.items()):
        print(f'  {q:<32s} {v:>20,.0f}')
    print(f'  {"TOTAL":<32s} {sum(totals.values()):>20,.0f}')

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            'data', 's2_kamino_v3_flares.json')
    with open(out_path, 'w') as f:
        json.dump({w: pq for w, pq in results.items()}, f, indent=2)
    print(f'\nSaved {len(results)} wallets to {out_path}')

    # Optionally write to wallet_quests + quest_cache. Default OFF — pass --write to enable.
    write_to_db = '--write' in sys.argv
    if write_to_db:
        WALKER_QUESTS = ['S2_KAMINO_LEND_USX','S2_KAMINO_LEND_EUSX','S2_KAMINO_LEND_USDG',
                         'S2_KAMINO_BORROW_USX','S2_KAMINO_BORROW_USDG']
        walker_db.prune('walk_s2_kamino')
        rows = []
        for w, pq in results.items():
            for q, v in pq.items():
                if v > 0: rows.append((w, q, v))
        walker_db.upsert_many('walk_s2_kamino', rows)
        walker_db.sync_to_wallet_quests('walk_s2_kamino', WALKER_QUESTS)
        print(f'\n✓ wallet_quests: {len(rows)} rows under walker "walk_s2_kamino"')

        # Write quest_cache for EVERY obligation owner we walked. Stale v1
        # cache entries (with phantom multiply deposits) MUST be wiped first
        # — transform_kamino reads cache for every wallet, not just ours.
        _db.init()
        wiped = _db.conn().execute("DELETE FROM quest_cache WHERE quest_key='S2_KAMINO'").rowcount
        _db.conn().commit()
        print(f'✓ wiped {wiped} stale S2_KAMINO cache entries')
        n_cache = 0
        all_walked_users = set(obl_to_owner.values())   # 1351 unique owners
        for user in all_walked_users:
            user_snap = snapshots.get(user, {})   # may be missing if walker didn't fetch snapshot
            if not user_snap:
                # Pull snapshot now for wallets we didn't query (had 0 events).
                # Their cache should reflect current position so dashboard shows it.
                try: user_snap = get_user_snapshot(user)
                except Exception: user_snap = {}
            full_snap = {
                'kamino_supply_usx':  user_snap.get('kamino_supply_usx', 0) or 0,
                'kamino_supply_eusx': user_snap.get('kamino_supply_eusx', 0) or 0,
                'kamino_supply_usdg': user_snap.get('kamino_supply_usdg', 0) or 0,
                'kamino_borrow_usx':  user_snap.get('kamino_borrow_usx', 0) or 0,
                'kamino_borrow_usdg': user_snap.get('kamino_borrow_usdg', 0) or 0,
                'kamino_kvault_usx_usdg': 0,   # filled by walk_s2_kamino_strategy
            }
            evs = sorted(events_by_user.get(user, []), key=lambda e: e['ts'])
            payload = {
                'positions': full_snap,
                'events':    evs,
                '_watermark': {'slot': 0, 'ts': end_ts},
                '_walker': 'walk_s2_kamino_v3',
            }
            _db.put_cache(user, 'S2_KAMINO', payload, watermark_ts=end_ts)
            n_cache += 1
        print(f'✓ quest_cache: {n_cache} entries written (cleared stale v1 entries)')
    else:
        print('\n(Did NOT write to DB. Pass --write to promote v3 results.)')


if __name__ == '__main__':
    main()
