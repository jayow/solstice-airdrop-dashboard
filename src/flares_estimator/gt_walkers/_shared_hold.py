"""Shared TWAB extraction for HOLD_USX_* and HOLD_EUSX_*.

Single extract → 3 transforms per mint (daily / 1MO / 3MO).
"""
import os, sys, time
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))

from rpc_helper import rpc
import db
from ._base import S2_START_TS, S2_END_TS


def _list_atas(wallet: str, mint: str) -> list:
    r = rpc('getTokenAccountsByOwner', [wallet, {'mint': mint}, {'encoding': 'jsonParsed'}], timeout=15)
    return [a['pubkey'] for a in (r.get('result', {}).get('value', []) or [])]


def _walk_ata_sigs(ata: str) -> list:
    sigs = []; before = None
    for _ in range(10):
        params = [ata, {'limit': 1000, **({'before': before} if before else {})}]
        r = rpc('getSignaturesForAddress', params, timeout=20)
        page = r.get('result') or []
        if not page: break
        sigs.extend(page)
        before = page[-1]['signature']
        if len(page) < 1000: break
    sigs.sort(key=lambda s: s.get('blockTime') or 0)
    return sigs


def _post_balance(sig: str, ata: str):
    r = rpc('getTransaction', [sig, {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}], timeout=15)
    tx = r.get('result')
    if not tx: return None
    msg = tx['transaction']['message']
    keys = [k.get('pubkey') if isinstance(k, dict) else k for k in msg.get('accountKeys', [])]
    if ata not in keys: return None
    idx = keys.index(ata)
    post = next((b for b in (tx.get('meta', {}).get('postTokenBalances', []) or [])
                  if b.get('accountIndex') == idx), None)
    if not post: return None
    return float(post.get('uiTokenAmount', {}).get('uiAmount') or 0)


def is_hold_cache_stale(cached: dict | None, wallet: str, daily_quest: str) -> bool:
    """Return True if the cached HOLD entry is contradicted by wallet_quests
    (i.e. we previously credited the wallet for HOLD flares, but the cache
    now claims no ATAs were ever found). This catches the RPC-flake failure
    mode where one bad getTokenAccountsByOwner call poisons the 24h cache.
    """
    if not cached: return False
    raw = cached.get('raw') or {}
    if (raw.get('atas') or []) != []: return False
    # Cache claims no ATAs. Cross-check wallet_quests: if we have historical
    # flares for the matching DAILY quest, the cache is contradicting itself.
    try:
        import sqlite3 as _sq, os as _os
        _ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
        _con = _sq.connect(_os.path.join(_ROOT, 'data', 'solstice.db'))
        row = _con.execute('SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?', (wallet, daily_quest)).fetchone()
        _con.close()
        return bool(row and (row[0] or 0) > 0)
    except Exception:
        return False


def build_twab_timeline(wallet: str, mint: str) -> dict:
    """Walk every ATA owned by `wallet` for `mint` and produce a unified balance timeline.

    Returns: {'atas': [...], 'timeline': [[ts, balance_total], ...], 'last_event_ts': int}
    """
    atas = _list_atas(wallet, mint)
    end_ts = min(int(time.time()), S2_END_TS)
    if not atas:
        return {'atas': [], 'timeline': [[S2_START_TS, 0.0], [end_ts, 0.0]], 'last_event_ts': end_ts}

    per_ata = {}
    for ata in atas:
        sigs = _walk_ata_sigs(ata)
        if not sigs: continue
        # carry-in (balance just before S2)
        pre = [s for s in sigs if (s.get('blockTime') or 0) < S2_START_TS]
        carry = 0.0
        if pre:
            r = _post_balance(pre[-1]['signature'], ata)
            if r is not None: carry = r
        segs = [(S2_START_TS, carry)]
        for s in [s for s in sigs if S2_START_TS <= (s.get('blockTime') or 0) <= end_ts]:
            ts = s.get('blockTime') or 0
            bal = _post_balance(s['signature'], ata)
            if bal is None: continue
            if ts <= segs[-1][0]: continue
            segs.append((ts, bal))
        per_ata[ata] = segs

    all_ts = sorted({S2_START_TS, end_ts} | {ts for segs in per_ata.values() for ts, _ in segs})
    timeline = []
    for t in all_ts:
        total = 0.0
        for segs in per_ata.values():
            last = 0.0
            for ts, b in segs:
                if ts <= t: last = b
                else: break
            total += last
        if not timeline or total != timeline[-1][1] or t == end_ts:
            timeline.append([t, total])
    return {'atas': atas, 'timeline': timeline, 'last_event_ts': end_ts}


def integrate_daily(timeline: list, mult: int, usd_per_token: float, end_ts: int) -> float:
    """daily TWAB: balance × usd × mult × dt_days, with tail extension to end_ts."""
    flares = 0.0
    if not timeline: return 0.0
    for i in range(len(timeline) - 1):
        t0, b0 = timeline[i]; t1, _ = timeline[i + 1]
        if t1 > end_ts: t1 = end_ts
        if t1 <= t0 or b0 <= 0: continue
        flares += b0 * usd_per_token * mult * (t1 - t0) / 86400.0
    last_t, last_b = timeline[-1]
    if last_t < end_ts and last_b > 0:
        flares += last_b * usd_per_token * mult * (end_ts - last_t) / 86400.0
    return flares


def integrate_qualified_bonus(timeline: list, min_bal: float, qualify_days: int,
                                 mult: int, usd_per_token: float, end_ts: int) -> float:
    """Bonus scales with actual balance once continuous-hold ≥ min_bal reaches qualify_days.
    Run resets on dip below min_bal."""
    if min_bal <= 0 or qualify_days <= 0 or not timeline: return 0.0
    qualify_sec = qualify_days * 86400
    flares = 0.0
    segments = []
    for i in range(len(timeline) - 1):
        t0, bal = timeline[i]; t1, _ = timeline[i + 1]
        if t1 > end_ts: t1 = end_ts
        if t1 > t0: segments.append((t0, bal, t1))
    last_t, last_b = timeline[-1]
    if last_t < end_ts: segments.append((last_t, last_b, end_ts))

    run_start = None
    for ts0, bal, ts1 in segments:
        if bal >= min_bal:
            if run_start is None: run_start = ts0
            qualify_ts = run_start + qualify_sec
            earn_start = max(ts0, qualify_ts)
            if earn_start < ts1:
                flares += bal * usd_per_token * mult * (ts1 - earn_start) / 86400.0
        else:
            run_start = None
    return flares


def discover_universe_for_mint(mint: str) -> list:
    """Enumerate every owner of an SPL token account for `mint` across BOTH token
    programs. Ground truth: every wallet that has ever received this token has
    a token account."""
    import base64, base58
    TOKEN_LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
    TOKEN_2022   = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
    owners = set()
    for prog, size in [(TOKEN_LEGACY, 165), (TOKEN_2022, None)]:
        filters = [{'memcmp': {'offset': 0, 'bytes': mint}}]
        if size: filters.insert(0, {'dataSize': size})
        try:
            r = rpc('getProgramAccounts', [prog, {
                'encoding': 'base64',
                'dataSlice': {'offset': 32, 'length': 40},
                'filters': filters,
            }], timeout=180)
            for a in (r.get('result') or []):
                d = base64.b64decode(a['account']['data'][0])
                if len(d) < 40: continue
                owner = base58.b58encode(d[:32]).decode()
                amount = int.from_bytes(d[32:40], 'little')
                if amount > 0: owners.add(owner)
        except Exception as e:
            print(f'  WARN discover {prog[:8]}.. {mint[:8]}..: {e}', flush=True)
    return sorted(owners)


def get_mint_supply(mint: str) -> float:
    """Current SPL mint supply (for cross-check)."""
    try:
        r = rpc('getAccountInfo', [mint, {'encoding': 'jsonParsed'}], timeout=10)
        info = r['result']['value']['data']['parsed']['info']
        return float(info['supply']) / (10 ** int(info['decimals']))
    except Exception: return 0.0
