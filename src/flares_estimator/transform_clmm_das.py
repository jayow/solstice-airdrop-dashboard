"""Extend in-range CLMM gating to wallets whose mint_positions weren't
captured in cached events.

For each wallet with positions[orca_*] > 0 or positions[raydium_*] > 0 in its
quest_cache entry, but no `mint_position` field in any cached event:
  1. Enumerate the wallet's currently-held NFT mints via getTokenAccountsByOwner
     (Token-2022 program — CLMM positions use Token-2022 NFTs).
  2. For each mint, derive BOTH the Orca position PDA and the Raydium position
     PDA — the wallet may hold positions in either protocol.
  3. Batch-fetch all candidate PDAs via getMultipleAccounts.
  4. Decode any that matched a CLMM program's expected layout; identify pool;
     check tickLower <= current_pool_tick < tickUpper.
  5. Scale wallet_quests for that wallet × pool by the in-range fraction
     (liquidity-weighted), same as transform_clmm_inrange.py.

Reads RPC heavily — one getTokenAccountsByOwner call per wallet, then chunked
batch fetches. With the configured RPC pool + 16 workers, expect ~10–15 min
for ~3200 wallets.
"""
import os, sys, json, time, base64, struct
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as fdb
from rpc_helper import rpc
from solders.pubkey import Pubkey


# QuickNode endpoint, used directly (no rotation) for the heavy batch fetch.
# Tonight's failure mode: the standard rotation tried Helius first, exhausted
# its quota, cascaded through all 8 endpoints, and started returning {}.
# Pinning the bulk work to QN's free-trial credits (10M, plenty for our
# ~30k position lookups) avoids the cascade.
_QN_ENDPOINT = None
def _qn_endpoint() -> str | None:
    global _QN_ENDPOINT
    if _QN_ENDPOINT is not None: return _QN_ENDPOINT
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
    try:
        for line in open(env_path):
            line = line.strip()
            if '=' not in line or line.startswith('#'): continue
            k, v = line.split('=', 1)
            if k.strip().lower().startswith('quicknode'):
                _QN_ENDPOINT = v.strip()
                return _QN_ENDPOINT
    except Exception: pass
    return None


def _qn_get_multiple(addrs: list[str], timeout: int = 30) -> list:
    """Direct QN getMultipleAccounts. Returns a list (same length as addrs)
    where each element is the account dict or None (if doesn't exist)."""
    ep = _qn_endpoint()
    if not ep: return []
    body = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
            "params": [addrs, {"encoding": "base64"}]}
    try:
        r = requests.post(ep, json=body, timeout=timeout)
        j = r.json()
        return (j.get('result') or {}).get('value') or []
    except Exception:
        return []

WHIRL_PROG   = Pubkey.from_string("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc")
RAYDIUM_CLMM = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
TOKEN_PROG   = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022   = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

ORCA_POOLS = {
    '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix': ('orca_usx_usdc', 'S2_ORCA_USX_USDC'),
    'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf': ('orca_eusx_usx', 'S2_ORCA_EUSX_USX'),
    'J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q': ('orca_usx_usdg', 'S2_ORCA_USX_USDG'),
}
RAYDIUM_POOLS = {
    'EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6': ('raydium_usx_usdc', 'S2_RAYDIUM_USX_USDC'),
    'BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n': ('raydium_eusx_usx', 'S2_RAYDIUM_EUSX_USX'),
}


def _orca_pda(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"position", bytes(Pubkey.from_string(mint))], WHIRL_PROG)
    return str(pda)


def _raydium_pda(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"position", bytes(Pubkey.from_string(mint))], RAYDIUM_CLMM)
    return str(pda)


def _decode_orca(data: bytes):
    if len(data) < 96: return None
    try:
        from solders.pubkey import Pubkey as P
        whirl = str(P(data[8:40]))
        liquidity = int.from_bytes(data[72:88], 'little')
        tl = struct.unpack('<i', data[88:92])[0]
        tu = struct.unpack('<i', data[92:96])[0]
        return {'pool': whirl, 'liquidity': liquidity, 'tick_lower': tl, 'tick_upper': tu}
    except Exception: return None


def _decode_raydium(data: bytes):
    if len(data) < 97: return None
    try:
        from solders.pubkey import Pubkey as P
        pool = str(P(data[41:73]))
        tl = struct.unpack('<i', data[73:77])[0]
        tu = struct.unpack('<i', data[77:81])[0]
        liq = int.from_bytes(data[81:97], 'little')
        return {'pool': pool, 'liquidity': liq, 'tick_lower': tl, 'tick_upper': tu}
    except Exception: return None


def _wallet_nfts(wallet: str) -> list[str]:
    """Return all mint pubkeys for NFTs (supply=1, decimals=0) the wallet holds
    across SPL Token + Token-2022 programs."""
    out = []
    for prog in (TOKEN_2022, TOKEN_PROG):
        try:
            r = rpc("getTokenAccountsByOwner",
                    [wallet, {"programId": prog}, {"encoding": "jsonParsed"}],
                    timeout=30)
        except Exception: continue
        for acc in (r.get("result", {}).get("value", []) or []):
            data = acc.get("account", {}).get("data")
            if not isinstance(data, dict): continue
            info = (data.get("parsed") or {}).get("info") or {}
            amt = info.get("tokenAmount", {}) or {}
            if amt.get("decimals") == 0 and int(amt.get("amount") or 0) >= 1:
                mint = info.get("mint")
                if mint: out.append(mint)
    return out


def _live_tick(pool_addr: str, protocol: str) -> int | None:
    import requests, math
    try:
        if protocol == 'orca':
            r = requests.get(f'https://api.orca.so/v2/solana/pools/{pool_addr}', timeout=10).json()
            return (r.get('data') or {}).get('tickCurrentIndex')
        r = requests.get(f'https://api-v3.raydium.io/pools/info/ids?ids={pool_addr}', timeout=10).json()
        d = (r.get('data') or [None])[0]
        if not d: return None
        return int(round(math.log(float(d.get('price') or 1.0)) / math.log(1.0001)))
    except Exception: return None


def main():
    fdb.init()
    con = fdb.conn()

    # Pool ticks (5 calls, free APIs)
    pool_ticks = {a: _live_tick(a, 'orca') for a in ORCA_POOLS}
    pool_ticks.update({a: _live_tick(a, 'raydium') for a in RAYDIUM_POOLS})
    print(f'Pool ticks: {pool_ticks}', flush=True)

    # 1. Find wallets with positions but no captured mints (per protocol)
    def collect_unverified(cache_key: str) -> set[str]:
        unv = set()
        for r in con.execute(f"SELECT wallet, raw_json FROM quest_cache WHERE quest_key=?", (cache_key,)):
            try: raw = json.loads(r['raw_json'])
            except Exception: continue
            pos = raw.get('positions') or {}
            if not any((v or 0) > 0 for v in pos.values()): continue
            mints = set(e.get('mint_position') for e in (raw.get('events') or []) if e.get('mint_position'))
            if not mints: unv.add(r['wallet'])
        return unv

    orca_unv = collect_unverified('S2_ORCA')
    ray_unv  = collect_unverified('S2_RAYDIUM')
    all_unv = orca_unv | ray_unv
    print(f'Orca unverified: {len(orca_unv):,}', flush=True)
    print(f'Raydium unverified: {len(ray_unv):,}', flush=True)
    print(f'Union: {len(all_unv):,} unique wallets', flush=True)

    # 2. Enumerate NFTs per wallet (parallel)
    print('Enumerating wallet NFTs...', flush=True)
    mints_by_wallet: dict[str, list[str]] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_wallet_nfts, w): w for w in all_unv}
        done = 0
        for fut in as_completed(futs):
            w = futs[fut]
            try: mints_by_wallet[w] = fut.result()
            except Exception: mints_by_wallet[w] = []
            done += 1
            if done % 200 == 0:
                print(f'  {done}/{len(all_unv)}  ({time.time()-t0:.0f}s)', flush=True)
    n_with_nfts = sum(1 for v in mints_by_wallet.values() if v)
    total_nfts = sum(len(v) for v in mints_by_wallet.values())
    print(f'  done. {n_with_nfts}/{len(all_unv)} wallets had NFTs, {total_nfts} total mints', flush=True)

    # 3. Derive position PDAs for both protocols. Track which PDA came from
    #    which mint + wallet so we can route results.
    print('Deriving position PDAs (both Orca + Raydium)...', flush=True)
    pda_meta: dict[str, tuple[str, str, str]] = {}  # pda → (wallet, mint, protocol)
    for w, mints in mints_by_wallet.items():
        for m in mints:
            try:
                pda_meta[_orca_pda(m)]    = (w, m, 'orca')
                pda_meta[_raydium_pda(m)] = (w, m, 'raydium')
            except Exception:
                continue
    print(f'  {len(pda_meta):,} candidate PDAs (most will return null)', flush=True)

    # 4. Batch-fetch PDAs. QN free tier limits getMultipleAccounts to 5
    # accounts per call, so we chunk to 5 and parallelize lightly. Still well
    # within the 10M free credit budget (~6k calls × 35 credits = 210k).
    pdas = list(pda_meta.keys())
    print(f'Batch-fetching {len(pdas):,} accounts (QN chunks of 5)...', flush=True)
    t0 = time.time()
    chunks = [pdas[i:i+5] for i in range(0, len(pdas), 5)]
    accounts: dict[str, dict | None] = {}
    def fetch(chunk):
        # Pin to QuickNode directly — bypasses the rotation that ate Helius
        # tonight. QN free trial = 10M credits, 15 RPS; plenty for ~30k
        # position lookups (~39k credits).
        for attempt in range(4):
            vals = _qn_get_multiple(chunk)
            if vals: return chunk, vals
            time.sleep(0.5 * (1 + attempt))
        return chunk, []
    # 4 concurrent workers — QN free tier is 15 RPS sustained, so 4 workers
    # paced naturally stays under. Now that we pin to QN directly (not
    # rotation), endpoint exhaustion isn't an issue.
    done = 0
    empty_count = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch, c) for c in chunks]
        for fut in as_completed(futs):
            chunk, vals = fut.result()
            if not vals: empty_count += 1
            for a, v in zip(chunk, vals): accounts[a] = v
            done += 1
            if done % 200 == 0:
                print(f'  {done}/{len(chunks)} batches  ({time.time()-t0:.0f}s)  empty={empty_count}  accounts_so_far={len(accounts)}', flush=True)
    print(f'  fetched in {time.time()-t0:.0f}s', flush=True)

    # 5. Decode + bucket per (wallet, pool)
    n_non_null = sum(1 for a in accounts.values() if a)
    print(f'  accounts populated: {len(accounts):,}, non-null: {n_non_null:,}', flush=True)
    by_wallet_pool = defaultdict(lambda: {'total': 0, 'in_range': 0})
    n_decoded = 0; n_open = 0; n_inrange = 0
    for pda, acc in accounts.items():
        if not acc: continue
        meta = pda_meta.get(pda)
        if not meta: continue
        wallet, mint, proto = meta
        owner = acc.get('owner')
        if proto == 'orca' and owner != str(WHIRL_PROG): continue
        if proto == 'raydium' and owner != str(RAYDIUM_CLMM): continue
        try: data = base64.b64decode(acc['data'][0])
        except Exception: continue
        pos = _decode_orca(data) if proto == 'orca' else _decode_raydium(data)
        if not pos: continue
        n_decoded += 1
        if pos['liquidity'] <= 0: continue
        n_open += 1
        pool_map = ORCA_POOLS if proto == 'orca' else RAYDIUM_POOLS
        if pos['pool'] not in pool_map: continue
        tn = pool_ticks.get(pos['pool'])
        in_range = (tn is not None and pos['tick_lower'] <= tn < pos['tick_upper'])
        by_wallet_pool[(wallet, pos['pool'])]['total'] += pos['liquidity']
        if in_range:
            by_wallet_pool[(wallet, pos['pool'])]['in_range'] += pos['liquidity']
            n_inrange += 1
    print(f'  decoded={n_decoded:,} open={n_open:,} in_range={n_inrange:,}', flush=True)

    # 6. Apply scaling per (wallet, pool). SAFETY: only modify wallet_quests
    # for (wallet, pool) pairs where we actually decoded a CLMM position
    # belonging to that pool. Wallets we couldn't decode are left at the
    # walker's prior value — never zero them, since absence of decoded
    # data doesn't prove the wallet has no positions.
    n_scaled = 0
    old_sum = defaultdict(float); new_sum = defaultdict(float)
    all_pools = list(ORCA_POOLS.items()) + list(RAYDIUM_POOLS.items())
    # Only touch (wallet, pool) pairs in by_wallet_pool (proven decoded state).
    for (wallet, pool_addr), wp in by_wallet_pool.items():
        if wp['total'] <= 0: continue
        quest = next((q for a, (_, q) in all_pools if a == pool_addr), None)
        if not quest: continue
        prev = con.execute(
            'SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?',
            (wallet, quest)
        ).fetchone()
        prev_v = float((prev['flares'] if prev else 0) or 0)
        old_sum[quest] += prev_v
        frac = wp['in_range'] / wp['total']
        new_v = prev_v * frac
        new_sum[quest] += new_v
        if abs(new_v - prev_v) > 0.5:
            fdb.upsert_wallet_quest(wallet, quest, new_v, source='clmm_inrange_das')
            n_scaled += 1
    con.commit()

    print(f'\nScaled {n_scaled} wallet_quest rows.')
    print(f'{"Quest":<32} {"OLD":>16}  {"NEW":>16}  {"delta":>16}')
    for _, (_, q) in all_pools:
        d = new_sum[q] - old_sum[q]
        print(f'{q:<32} {old_sum[q]:>16,.0f}  {new_sum[q]:>16,.0f}  {d:>+16,.0f}')


if __name__ == '__main__':
    main()
