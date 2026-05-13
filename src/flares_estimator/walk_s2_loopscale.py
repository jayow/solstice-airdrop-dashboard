"""Walk S2 Loopscale activity per wallet.

Quests:
  S2_LOOPSCALE_BORROW_USX (1×)  — sum over loans: principal_USD × dt × 1
  S2_LOOPSCALE_SUPPLY_USX_ONE (5×) — vault deposit USD × dt × 5

For BORROW: walk loan history via Loopscale API. Each loan has start/end ts.
For SUPPLY: walk USX ONE LP mint holders (token accounts), then per-ATA sig history.

Apply Solstice "minimum one day rewarded" rule.

Output: data/s2_loopscale_flares.json
"""
import os, sys, json, time, requests
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
from loopscale_extractor import get_loopscale_borrow_history, USX_ONE_LP_MINT, USX_ONE_VAULT
import walker_db

S2_START_TS = 1776038400
MIN_HOLD_DAYS = 1.0
USX_MINT = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
LOOP_API = 'https://tars.loopscale.com/v1'


def get_vault_share_value():
    """USX ONE vault share value = cumulativePrincipalDeposited / lpSupply."""
    r = requests.post(
        f'{LOOP_API}/markets/lending_vaults/info',
        json={'vaultAddresses':[USX_ONE_VAULT], 'page':0, 'pageSize':1},
        timeout=15
    ).json()
    items = r.get('lendVaults', [])
    if not items: return 0.0
    vault = items[0]['vault']
    try:
        lp_supply = float(vault['lpSupply']) / 1e6
        cum_dep = float(vault['cumulativePrincipalDeposited']) / 1e6
        return cum_dep / lp_supply if lp_supply > 0 else 0.0
    except Exception: return 0.0


def walk_borrow(now_ts: int):
    """For each Loopscale USX borrower (enumerated via API), integrate USD-days."""
    # Self-enumerate: page through Loopscale loans/info with USX principal filter,
    # collecting every unique borrower. Catches both active and historical loans.
    wallets = set()
    page = 0
    while True:
        try:
            r = requests.post(
                f'{LOOP_API}/markets/loans/info',
                json={'principalMints':[USX_MINT], 'page':page, 'pageSize':100},
                timeout=20
            ).json()
        except Exception:
            break
        items = r.get('items', []) or []
        if not items: break
        for loan in items:
            b = (loan.get('loan',{}) or {}).get('borrower') or loan.get('borrower')
            if b: wallets.add(b)
        if len(items) < 100: break
        page += 1
    # Also include any wallet from prior walker output (in case API doesn't return historical)
    try:
        for w in walker_db.wallets_with_quest_above('S2_LOOPSCALE_BORROW_USX', 0):
            wallets.add(w)
    except Exception: pass
    print(f'  walking borrow history for {len(wallets):,} wallets (self-enumerated)...', flush=True)

    def process(w):
        try:
            hist = get_loopscale_borrow_history(w)
        except Exception:
            return w, 0.0
        usd_days = 0.0
        for h in hist:
            start = h['start_ts']
            end = h['end_ts'] or now_ts
            # Intersect with S2 window
            s2s = max(start, S2_START_TS)
            s2e = min(end, now_ts)
            if s2e <= s2s: continue
            days = (s2e - s2s) / 86400
            if days < MIN_HOLD_DAYS: continue
            usd_days += h['principal_usx'] * days   # USX ~ $1
        return w, usd_days

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(process, w) for w in wallets]
        n_done = 0
        for fut in as_completed(futs):
            w, usd_days = fut.result()
            n_done += 1
            if n_done % 25 == 0: print(f'    {n_done}/{len(wallets)}', flush=True)
            if usd_days > 0:
                results[w] = usd_days * 1   # mult 1×
    total = sum(results.values())
    print(f'  borrow walk: {len(results)} wallets, total {total:,.0f} flares\n', flush=True)
    return results


def walk_supply(now_ts: int, share_value: float):
    """For each USX ONE LP holder, walk their ATA sig history.

    Loopscale users don't hold LP-USX-ONE in their wallet ATAs directly; the
    LP is custodied by a Loopscale VaultStake PDA owned by the Loopscale
    program. Each VaultStake stores the REAL user pubkey at offset 73 of its
    account data (disc e1228035a7efb66b). We need to re-key every PDA-owned
    holder back to the real user — otherwise flares get attributed to the
    Loopscale vault PDAs, which the dashboard filters out as PDAs, leaving
    real users with $0.
    """
    LOOPSCALE_PROGRAM = '1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78'
    VAULT_STAKE_DISC = 'e1228035a7efb66b'

    holders = []
    for prog in ['TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb']:
        r = rpc('getProgramAccounts', [prog, {
            'encoding': 'jsonParsed',
            'filters': [
                {'memcmp': {'offset': 0, 'bytes': USX_ONE_LP_MINT}}
            ]
        }], timeout=120)
        accs = r.get('result', []) or []
        for a in accs:
            info = a['account']['data']['parsed']['info']
            owner = info.get('owner')
            bal = float(info['tokenAmount']['uiAmount'] or 0)
            holders.append({'ata': a['pubkey'], 'owner': owner, 'current_bal': bal})

    # Re-key any owner that's a Loopscale VaultStake PDA → real user @ offset 73.
    import base64 as _b64, base58 as _b58
    n_rekeyed = 0
    for h in holders:
        try:
            r = rpc('getAccountInfo', [h['owner'], {'encoding': 'base64'}])
            v = r.get('result', {}).get('value') or {}
            if v.get('owner') != LOOPSCALE_PROGRAM: continue
            d = _b64.b64decode(v.get('data', ['', ''])[0])
            if len(d) < 105 or d[:8].hex() != VAULT_STAKE_DISC: continue
            real_user = _b58.b58encode(d[73:105]).decode()
            h['owner'] = real_user
            n_rekeyed += 1
        except Exception: continue

    nonzero = sum(1 for h in holders if h['current_bal'] > 0)
    print(f'  USX ONE LP-mint accounts: {len(holders)}  non-zero now: {nonzero}  re-keyed via VaultStake: {n_rekeyed}', flush=True)

    def process_ata(h):
        ata = h['ata']; owner = h['owner']; current_bal = h['current_bal']
        # Walk the FULL sig history (no S2 cutoff). Pre-S2 sigs are needed to
        # determine the balance at S2 start — otherwise wallets that deposited
        # pre-S2 and held through aren't anchored correctly. Without carry-in,
        # the first integration segment (S2_START → first S2 event) is dropped
        # and the wallet gets credit only after their first S2 sig.
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
        if not sigs and current_bal == 0: return owner, 0.0
        # Fetch all txs in parallel, recording post-balance per sig.
        def fetch(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
                return s, r.get('result')
            except: return s, None
        events = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch, s) for s in sigs]
            for fut in as_completed(futs):
                s, tx = fut.result()
                if not tx: continue
                if (tx['meta'] or {}).get('err'): continue
                pre = {(t['accountIndex'], t['mint']): t for t in tx['meta'].get('preTokenBalances', [])}
                post = {(t['accountIndex'], t['mint']): t for t in tx['meta'].get('postTokenBalances', [])}
                bal_after = None
                for k in set(pre)|set(post):
                    idx, mint = k
                    if mint != USX_ONE_LP_MINT: continue
                    pb = pre.get(k); pob = post.get(k)
                    if (pob or pb).get('owner') != owner: continue
                    bal_after = ((pob or pb).get('uiTokenAmount', {}) or {}).get('uiAmount') or 0
                if bal_after is not None:
                    events.append((s['blockTime'], float(bal_after)))
        events.sort(key=lambda x: x[0])
        # Derive carry-in: balance at S2_START. Last observed balance from any
        # pre-S2 event; if no pre-S2 events but the position predates S2 (no
        # events at all yet current_bal > 0), use current_bal as carry-in.
        pre_evs = [e for e in events if e[0] < S2_START_TS]
        s2_evs  = [e for e in events if e[0] >= S2_START_TS]
        if pre_evs:
            carry_in = pre_evs[-1][1]
        elif current_bal > 0 and not s2_evs:
            carry_in = current_bal   # held through S2 with no observed sigs
        else:
            carry_in = 0.0
        # Piecewise integrate: [S2_START, first_s2_event, ..., last_s2_event, now]
        usd_days = 0.0
        bal = carry_in
        prev_t = S2_START_TS
        for t, post_bal in s2_evs:
            dt = (t - prev_t) / 86400
            if dt > 0 and bal > 0:
                usd_days += bal * share_value * dt
            bal = post_bal
            prev_t = t
        # Tail from last event (or S2_START if none) to now
        if bal > 0 and prev_t < now_ts:
            dt = (now_ts - prev_t) / 86400
            if dt > 0:
                usd_days += bal * share_value * dt
        return owner, usd_days * 5   # mult 5×

    results = defaultdict(float)
    if not holders: return dict(results)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(process_ata, h) for h in holders]
        n_done = 0
        for fut in as_completed(futs):
            owner, flares = fut.result()
            results[owner] += flares
            n_done += 1
            if n_done % 25 == 0: print(f'    supply walk {n_done}/{len(holders)}', flush=True)
    total = sum(results.values())
    print(f'  supply walk: {sum(1 for v in results.values() if v>0)} wallets, total {total:,.0f} flares\n', flush=True)
    return dict(results)


def main():
    now_ts = int(time.time())
    print(f'S2 window: {(now_ts-S2_START_TS)/86400:.1f} days\n', flush=True)
    share_value = get_vault_share_value()
    print(f'USX ONE share value: ${share_value:.6f}\n', flush=True)

    print('=== BORROW (1×) ===', flush=True)
    borrow = walk_borrow(now_ts)

    print('=== SUPPLY (5×) ===', flush=True)
    supply = walk_supply(now_ts, share_value)

    # Combine
    out = defaultdict(dict)
    for w, v in borrow.items(): out[w]['S2_LOOPSCALE_BORROW_USX'] = v
    for w, v in supply.items():
        if v > 0: out[w]['S2_LOOPSCALE_SUPPLY_USX_ONE'] = v

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_loopscale_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    n_borrow = sum(1 for v in out.values() if v.get('S2_LOOPSCALE_BORROW_USX'))
    n_supply = sum(1 for v in out.values() if v.get('S2_LOOPSCALE_SUPPLY_USX_ONE'))
    print(f'\nSaved: borrow={n_borrow} wallets, supply={n_supply} wallets → {out_path}')

    # Write to DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS = ['S2_LOOPSCALE_BORROW_USX', 'S2_LOOPSCALE_SUPPLY_USX_ONE']
    walker_db.prune('walk_s2_loopscale')
    rows = []
    for w, pq in out.items():
        for q, v in pq.items():
            if v > 0: rows.append((w, q, v))
    walker_db.upsert_many('walk_s2_loopscale', rows)
    walker_db.sync_to_wallet_quests('walk_s2_loopscale', WALKER_QUESTS)
    print(f'DB: walker_outputs={len(rows)} rows; synced to wallet_quests')


if __name__ == '__main__':
    main()
