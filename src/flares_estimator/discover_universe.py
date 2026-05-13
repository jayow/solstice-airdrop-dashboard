"""Comprehensive S2 universe discovery.

Union of all wallets discoverable on-chain across:
  1. USX SPL holders (both legacy SPL + Token-2022)
  2. eUSX SPL holders
  3. Exponent YT position owners (USX-Jun26 + eUSX-Jun26 markets)
  4. Exponent LP holders (LP-mint holders, both token programs)
  5. Loopscale USX ONE LP holders
  6. Kamino obligation owners on Solstice market
  7. Kamino USDG/USX strategy share holders
  8. Orca whirlpool position NFT owners (3 S2 pools)
  9. Raydium CLMM position NFT owners (2 S2 pools)

Run daily to get a fresh universe. Output: data/universe_today.txt
"""
import os, sys, json, time, base64, base58, struct
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc

# Mint addresses
USX_MINT   = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT  = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT  = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'

# Programs
TOKEN_LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022   = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
EXPO_PROG    = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
WHIRL_PROG   = 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'
RAYDIUM_CLMM = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'
KLEND        = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'

SOLSTICE_KAMINO_MARKET = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'

# Solstice S2 markets/pools
USX_JUN26 = 'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm'
EUSX_JUN26 = 'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP'
ORCA_USX_USDC  = '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix'
ORCA_EUSX_USX  = 'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf'
ORCA_USX_USDG  = 'J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q'
RAY_USX_USDC   = 'EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6'
RAY_EUSX_USX   = 'BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n'

# LP / share mints
LP_MINT_USX  = 'BR2JKV9gPoJfX8A8DkFmo2yNQKCeGipg33oYaZ4EmjbW'
LP_MINT_EUSX = '4GT6g1iKx2TyYCkwt1tERkReQjSUuVE7uh14M5W8v2nn'
LOOP_USX_ONE_LP = '3PQotuGMnMgEXrErizQbzPPhSMb79xQgkEDn2hk2KPWn'
KAMINO_STRAT_SHARE = '4qkStdH1NPKMmxrTDbY8kzTkJorpGMd8GLxo81drv9Jz'

# Known PDA / protocol pool addresses to exclude (not user wallets)
EXCLUDE = {
    USX_JUN26, EUSX_JUN26, ORCA_USX_USDC, ORCA_EUSX_USX, ORCA_USX_USDG,
    RAY_USX_USDC, RAY_EUSX_USX, SOLSTICE_KAMINO_MARKET,
    # SY mint authorities (pool vaults)
    'xaMJ2ATJfWce7JZfwLeeZmBHmFadDUdLTqdpyn5hMiA',   # USX SY auth
    'GTFtrJGtTfH3C8c3jpkuraHpbwdaRR2jWuKuyfMb4mYh',  # eUSX SY auth
}


def get_mint_holders(mint: str) -> set:
    """Return set of owner wallets for an SPL mint across both Token programs.

    Uses dataSlice to only fetch the owner-pubkey bytes, which keeps the response
    size manageable for high-holder-count mints (USX has 55K+ token accounts).
    """
    owners = set()
    # Layout: SPL token account has mint at offset 0 (32B) + owner at offset 32 (32B).
    # Token-2022 same offsets for the base account; extensions live after.
    for prog, size in [(TOKEN_LEGACY, 165), (TOKEN_2022, None)]:
        filters = [{'memcmp': {'offset': 0, 'bytes': mint}}]
        if size: filters.insert(0, {'dataSize': size})
        try:
            r = rpc('getProgramAccounts', [prog, {
                'encoding': 'base64',
                'dataSlice': {'offset': 32, 'length': 72},   # owner (32) + amount (8) + delegate (32+?)
                'filters': filters,
            }], timeout=180)
            for a in r.get('result', []) or []:
                d = base64.b64decode(a['account']['data'][0])
                if len(d) < 40: continue
                owner = base58.b58encode(d[:32]).decode()
                # Amount is at offset 64 in full account → at offset 32 in our slice
                amount = int.from_bytes(d[32:40], 'little')
                if amount > 0 and owner not in EXCLUDE:
                    owners.add(owner)
        except Exception as e:
            print(f'  ERR {mint[:8]}.. {prog[:8]}..: {e}', flush=True)
    return owners


def get_exponent_yt_holders() -> set:
    """All wallets holding YT in either Solstice market (v1 + v2 disc, any size)."""
    YT_V2 = 'e35c92311d55475e'; YT_V1 = '69f125c8e002fc5a'
    yp_aliases = {}
    for mpk in [USX_JUN26, EUSX_JUN26]:
        r = rpc('getAccountInfo', [mpk, {'encoding':'base64'}])
        d = base64.b64decode(r['result']['value']['data'][0])
        yp_aliases[base58.b58encode(d[104:136]).decode()] = mpk
    owners = set()
    r = rpc('getProgramAccounts', [EXPO_PROG, {'encoding':'base64'}], timeout=300)
    for a in r.get('result', []) or []:
        try:
            d = base64.b64decode(a['account']['data'][0])
        except: continue
        if len(d) < 80: continue
        disc = d[:8].hex()
        if disc not in (YT_V1, YT_V2): continue
        yp_alias = base58.b58encode(d[40:72]).decode()
        if yp_alias not in yp_aliases: continue
        authority = base58.b58encode(d[8:40]).decode()
        owners.add(authority)
    return owners - EXCLUDE


def get_kamino_obligation_owners() -> set:
    """All wallets with obligation accounts on Solstice Kamino market."""
    owners = set()
    r = rpc('getProgramAccounts', [KLEND, {
        'encoding': 'base64',
        'dataSlice': {'offset': 64, 'length': 32},   # owner field
        'filters': [{'memcmp': {'offset': 32, 'bytes': SOLSTICE_KAMINO_MARKET}}]
    }], timeout=60)
    for a in r.get('result', []) or []:
        try:
            d = base64.b64decode(a['account']['data'][0])
            owner = base58.b58encode(d[:32]).decode()
            if owner not in EXCLUDE: owners.add(owner)
        except: continue
    return owners


def get_whirlpool_owners(pool: str) -> set:
    """All wallets holding active position NFTs in an Orca whirlpool."""
    POSITION_DISC = 'aabc8fe47a40f7d0'
    owners = set()
    r = rpc('getProgramAccounts', [WHIRL_PROG, {
        'encoding': 'base64',
        'filters': [
            {'dataSize': 216},
            {'memcmp': {'offset': 8, 'bytes': pool}}
        ]
    }], timeout=120)
    accs = r.get('result', []) or []
    # For each position, find current NFT holder
    def find_owner(mint):
        try:
            r = rpc('getTokenLargestAccounts', [mint], timeout=8)
            for h in r.get('result', {}).get('value', []):
                if float(h.get('uiAmount') or 0) >= 1:
                    r2 = rpc('getAccountInfo', [h['address'], {'encoding':'jsonParsed'}], timeout=8)
                    v = r2.get('result',{}).get('value')
                    if v:
                        info = (v.get('data',{}).get('parsed',{}) or {}).get('info',{}) or {}
                        return info.get('owner')
        except: return None
    mints = []
    for a in accs:
        d = base64.b64decode(a['account']['data'][0])
        if d[:8].hex() != POSITION_DISC: continue
        L = int.from_bytes(d[72:88], 'little')
        if L == 0: continue
        mints.append(base58.b58encode(d[40:72]).decode())
    with ThreadPoolExecutor(max_workers=12) as ex:
        for owner in ex.map(find_owner, mints):
            if owner and owner not in EXCLUDE: owners.add(owner)
    return owners


def get_raydium_owners(pool: str) -> set:
    POSITION_DISC = '466f967ee60f1975'
    owners = set()
    r = rpc('getProgramAccounts', [RAYDIUM_CLMM, {
        'encoding': 'base64',
        'filters': [
            {'dataSize': 281},
            {'memcmp': {'offset': 41, 'bytes': pool}}
        ]
    }], timeout=120)
    accs = r.get('result', []) or []
    def find_owner(mint):
        try:
            r = rpc('getTokenLargestAccounts', [mint], timeout=8)
            for h in r.get('result', {}).get('value', []):
                if float(h.get('uiAmount') or 0) >= 1:
                    r2 = rpc('getAccountInfo', [h['address'], {'encoding':'jsonParsed'}], timeout=8)
                    v = r2.get('result',{}).get('value')
                    if v:
                        info = (v.get('data',{}).get('parsed',{}) or {}).get('info',{}) or {}
                        return info.get('owner')
        except: return None
    mints = []
    for a in accs:
        d = base64.b64decode(a['account']['data'][0])
        if d[:8].hex() != POSITION_DISC: continue
        L = int.from_bytes(d[81:97], 'little')
        if L == 0: continue
        mints.append(base58.b58encode(d[9:41]).decode())
    with ThreadPoolExecutor(max_workers=12) as ex:
        for owner in ex.map(find_owner, mints):
            if owner and owner not in EXCLUDE: owners.add(owner)
    return owners


def main():
    print(f'=== Universe discovery {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")} ===\n', flush=True)
    universe = set()
    sources = {}

    t = time.time()
    print('[1/6] USX SPL holders…', flush=True)
    s = get_mint_holders(USX_MINT)
    sources['usx_holders'] = len(s)
    universe |= s
    print(f'      {len(s):,} ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    t = time.time()
    print('[2/6] eUSX SPL holders…', flush=True)
    s = get_mint_holders(EUSX_MINT)
    sources['eusx_holders'] = len(s)
    universe |= s
    print(f'      {len(s):,} ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    t = time.time()
    print('[3/6] Exponent YT holders…', flush=True)
    s = get_exponent_yt_holders()
    sources['exponent_yt'] = len(s)
    universe |= s
    print(f'      {len(s):,} ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    t = time.time()
    print('[4/6] Kamino obligation owners…', flush=True)
    s = get_kamino_obligation_owners()
    sources['kamino_obls'] = len(s)
    universe |= s
    print(f'      {len(s):,} ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    t = time.time()
    print('[5/6] LP / share mint holders (Exponent LP, Loopscale, Kamino strategy)…', flush=True)
    n_before = len(universe)
    for m in [LP_MINT_USX, LP_MINT_EUSX, LOOP_USX_ONE_LP, KAMINO_STRAT_SHARE]:
        universe |= get_mint_holders(m)
    sources['lp_share_mints'] = len(universe) - n_before
    print(f'      +{sources["lp_share_mints"]:,} new ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    t = time.time()
    print('[6/6] Orca + Raydium CLMM position NFTs…', flush=True)
    n_before = len(universe)
    for pool in [ORCA_USX_USDC, ORCA_EUSX_USX, ORCA_USX_USDG]:
        universe |= get_whirlpool_owners(pool)
    for pool in [RAY_USX_USDC, RAY_EUSX_USX]:
        universe |= get_raydium_owners(pool)
    sources['clmm_positions'] = len(universe) - n_before
    print(f'      +{sources["clmm_positions"]:,} new ({time.time()-t:.1f}s, universe {len(universe):,})', flush=True)

    print(f'\n=== Final universe: {len(universe):,} wallets ===', flush=True)
    for src, n in sources.items(): print(f'  {src}: {n:,}')

    # Save universe
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_path = os.path.join(root, 'data', 'universe_today.txt')
    with open(out_path, 'w') as f:
        for w in sorted(universe): f.write(w + '\n')
    print(f'\nWrote {out_path}')

    # Snapshot stats
    snap_path = os.path.join(root, 'data', 'universe_snapshots.jsonl')
    with open(snap_path, 'a') as f:
        f.write(json.dumps({
            'ts': datetime.now(UTC).isoformat(),
            'total': len(universe),
            'sources': sources,
        }) + '\n')


if __name__ == '__main__':
    main()
