"""Proactive missing-wallet discovery.

For every quest-relevant token mint (USX, eUSX, YT-USX-Jun26, YT-eUSX-Jun26,
LP-USX-Jun26, LP-eUSX-Jun26), enumerate ALL current on-chain holders via
`getProgramAccounts(TOKEN_PROGRAM, memcmp(mint))`. Cross-reference against
`wallet_quests` to find wallets that:

  - Hold the token on-chain (= should be earning flares)
  - But show 0 in wallet_quests (= walker missed them)

For each such wallet, run the corresponding extractor with force_refresh=True
to recover them.

This is the proactive complement to the reactive `retry_empty_caches.py` —
instead of waiting for someone to report a missed wallet, we proactively
diff on-chain holders against our DB and recover the gap.

Run periodically (daily ideal; weekly fine):
  python3 src/flares_estimator/discover_missing.py [--dry-run] [--mint MINT]
"""
import os, sys, time, json, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db as fdb
from quests import hold_usx, hold_eusx, exponent_yt, exponent_lp
from snapshot_ts import last_snapshot_ts

# (mint, quest_code_signaling_activity, extractor_class) — wallets currently
# holding `mint` should have non-zero `quest_code` in wallet_quests.
TARGETS = [
    {
        'label':     'USX',
        'mint':      '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',
        'quest':     'S2_HOLD_USX_DAILY',
        'extractor': hold_usx.HoldUSXExtractor,
    },
    {
        'label':     'eUSX',
        'mint':      '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
        'quest':     'S2_HOLD_EUSX_DAILY',
        'extractor': hold_eusx.HoldEUSXExtractor,
    },
    {
        'label':     'LP-USX-Jun26',
        'mint':      'BR2JKV9gPoJfX8A8DkFmo2yNQKCeGipg33oYaZ4EmjbW',
        'quest':     'S2_EXPONENT_LP_USX_JUN26',
        'extractor': exponent_lp.ExponentLPExtractor,
    },
    {
        'label':     'LP-eUSX-Jun26',
        'mint':      '4GT6g1iKx2TyYCkwt1tERkReQjSUuVE7uh14M5W8v2nn',
        'quest':     'S2_EXPONENT_LP_EUSX_JUN26',
        'extractor': exponent_lp.ExponentLPExtractor,
    },
    # YT mints are deterministic from market PDAs but cached — we'll resolve at runtime
]

# YT mints (look up dynamically since they're not constants)
YT_MARKETS = [
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm',  # USX-Jun26
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP',  # eUSX-Jun26
]

SOLSTICE_PROTOCOL_PDAS = {  # quietly skip these — not real users
    # Populated dynamically from `wallets` table classification
}

TOKEN_PROGRAM      = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'


def enumerate_holders(mint: str) -> dict:
    """Return {owner_wallet: total_balance} for all current holders of `mint`,
    via getProgramAccounts(TOKEN_PROGRAM, memcmp(mint at offset 0))."""
    holders: dict[str, float] = {}
    # Try both Token and Token-2022 programs (LP/YT often Token-2022)
    for prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            r = rpc('getProgramAccounts', [prog, {
                'encoding': 'jsonParsed',
                'filters': [
                    {'dataSize': 165 if prog == TOKEN_PROGRAM else 182},   # token-account size
                    {'memcmp': {'offset': 0, 'bytes': mint}},
                ],
            }], timeout=120, force_refresh=True)
        except Exception as e:
            print(f'  WARN getProgramAccounts({prog[:6]}) failed: {e}')
            continue
        for acc in (r.get('result') or []):
            try:
                info = acc.get('account', {}).get('data', {}).get('parsed', {}).get('info', {})
                owner = info.get('owner')
                amt = float((info.get('tokenAmount') or {}).get('uiAmount') or 0)
                if not owner or amt <= 0: continue
                holders[owner] = holders.get(owner, 0) + amt
            except Exception: continue
    return holders


def find_yt_mints() -> list[str]:
    """Resolve current YT mint pubkeys from each Exponent market PDA.
    YT mint is stored at offset 40 of the market account."""
    import base64, base58
    out = []
    for market_pk in YT_MARKETS:
        try:
            r = rpc('getAccountInfo', [market_pk, {'encoding': 'base64'}], force_refresh=True)
            v = (r.get('result') or {}).get('value')
            if not v: continue
            data = base64.b64decode(v['data'][0])
            mint = base58.b58encode(data[40:72]).decode()
            out.append({'label': f'YT @ {market_pk[:6]}', 'mint': mint, 'market': market_pk})
        except Exception as e:
            print(f'  WARN resolving YT mint for {market_pk[:8]}: {e}')
    return out


def find_missing_for_target(target: dict, dry_run: bool = False) -> dict:
    """Enumerate holders, diff against wallet_quests, return missing wallets."""
    label = target['label']
    mint  = target['mint']
    quest = target['quest']
    print(f'\n=== {label} (mint {mint[:10]}…) ===', flush=True)
    t0 = time.time()
    print(f'  enumerating holders via getProgramAccounts...', flush=True)
    holders = enumerate_holders(mint)
    print(f'  found {len(holders):,} on-chain holders ({time.time()-t0:.0f}s)', flush=True)
    if not holders: return {'missing': [], 'recovered': 0}

    # Pull current wallet_quests state for this quest
    con = fdb.conn()
    have = {r['wallet']: float(r['flares'] or 0)
            for r in con.execute('SELECT wallet, flares FROM wallet_quests WHERE quest=? AND flares > 0', (quest,))}
    # Pull classification for filtering known PDAs
    excluded = set(r['wallet'] for r in con.execute(
        "SELECT wallet FROM wallets WHERE classification IN ('pda','pda_or_uninit','pda_protocol')"))
    print(f'  DB shows {len(have):,} wallets earning {quest}', flush=True)

    candidates = []
    for w, bal in holders.items():
        if w in excluded: continue
        if have.get(w, 0) > 0: continue
        candidates.append((w, bal))
    print(f'  candidate misses (before PDA filter): {len(candidates):,}', flush=True)
    # Filter out PDAs: only keep wallets where getAccountInfo.owner = SystemProgram.
    # Top USX holders include yield_vault PDAs and other internal pools we
    # shouldn't credit as users. SystemProgram-owned = real user keypair.
    SYSTEM_PROGRAM = '11111111111111111111111111111111'
    print(f'  filtering PDAs via getAccountInfo (concurrent, batched)...', flush=True)
    t1 = time.time()
    missing = []
    def _is_user(w):
        try:
            r = rpc('getAccountInfo', [w, {'encoding': 'base64'}], timeout=15, force_refresh=True)
            v = (r.get('result') or {}).get('value')
            if v is None: return True  # uninit but token-holder — could be real user (no SOL)
            return v.get('owner') == SYSTEM_PROGRAM
        except Exception:
            return False  # err on the side of skip
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (w, b), is_u in zip(candidates, pool.map(_is_user, [w for w, _ in candidates])):
            if is_u: missing.append((w, b))
    print(f'  user-only missing (after PDA filter): {len(missing):,} ({time.time()-t1:.0f}s)', flush=True)
    if missing[:5]:
        for w, b in sorted(missing, key=lambda x: -x[1])[:5]:
            print(f'    {w}  bal={b:.4f}')

    if dry_run or not missing:
        return {'missing': missing, 'recovered': 0}

    # Force-refresh each missing wallet via the matching extractor.
    print(f'  force-refreshing {len(missing)} wallets...', flush=True)
    ext = target['extractor']()
    snap = last_snapshot_ts()
    recovered = 0
    new_flares = 0.0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_safe_run, ext, w, snap): w for w, _ in missing}
        for i, fut in enumerate(as_completed(futs), 1):
            w = futs[fut]
            out = fut.result() or {}
            for q, v in out.items():
                v = float(v or 0)
                if v > 0:
                    fdb.upsert_wallet_quest(w, q, v, source='discover_missing')
                    new_flares += v
                    recovered += 1
            if i % 50 == 0 or i == len(missing):
                print(f'    {i}/{len(missing)}  recovered_rows={recovered}  +{new_flares:,.0f}f', flush=True)
    con.commit()
    print(f'  DONE: recovered {recovered} quest rows, +{new_flares:,.0f} flares for {label}', flush=True)
    return {'missing': missing, 'recovered': recovered, 'flares_added': new_flares}


def _safe_run(ext, wallet, now_ts):
    try: return ext.run(wallet, now_ts, force_refresh=True)
    except Exception as e: return {'_error': str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help="List missing wallets but don't refresh")
    ap.add_argument('--only', help="Only this label (e.g. USX, eUSX, LP-USX-Jun26)")
    args = ap.parse_args()
    fdb.init()

    # Build full target list (static + dynamically-resolved YT mints)
    targets = list(TARGETS)
    for ym in find_yt_mints():
        targets.append({
            'label':     ym['label'],
            'mint':      ym['mint'],
            'quest':     ('S2_EXPONENT_YIELD_USX_JUN26' if ym['market'].startswith('Bxbi')
                          else 'S2_EXPONENT_YIELD_EUSX_JUN26'),
            'extractor': exponent_yt.ExponentYTExtractor,
        })

    total_missing = 0; total_recovered = 0; total_flares = 0.0
    for t in targets:
        if args.only and t['label'] != args.only: continue
        r = find_missing_for_target(t, args.dry_run)
        total_missing += len(r['missing'])
        total_recovered += r.get('recovered', 0)
        total_flares += r.get('flares_added', 0)

    print()
    print(f'=== GRAND TOTAL ===')
    print(f'  Missing wallets across all targets: {total_missing:,}')
    if not args.dry_run:
        print(f'  Recovered quest rows: {total_recovered:,}')
        print(f'  New flares added: +{total_flares:,.0f}')


if __name__ == '__main__':
    main()
