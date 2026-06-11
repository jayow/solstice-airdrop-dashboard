"""Shared TWAB extraction for HOLD_USX_* and HOLD_EUSX_*.

Single extract → 3 transforms per mint (daily / 1MO / 3MO).
"""
import os, sys, time
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))

from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
import db
from ._base import S2_START_TS, S2_END_TS, USX_MINT, EUSX_MINT


# Canonical ATA derivation — gives us a deterministic fallback when
# getTokenAccountsByOwner returns silently empty (RPC retry-exhausted).
# The canonical ATA's signature history survives account closure on Solana,
# so walking it can still reconstruct HOLD flares even for closed accounts.
try:
    from solders.pubkey import Pubkey as _Pubkey
    _TOKEN_PROG = _Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    _ATOK_PROG  = _Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    def _canonical_ata(wallet: str, mint: str) -> str | None:
        try:
            pda, _ = _Pubkey.find_program_address(
                [bytes(_Pubkey.from_string(wallet)), bytes(_TOKEN_PROG),
                 bytes(_Pubkey.from_string(mint))], _ATOK_PROG)
            return str(pda)
        except Exception:
            return None
except Exception:
    def _canonical_ata(wallet: str, mint: str) -> str | None:
        return None


_MINT_CACHE_KEY = {USX_MINT: 'S2_HOLD_USX', EUSX_MINT: 'S2_HOLD_EUSX'}


# Map mint → S2 orderbook(s) whose SY-side escrow contributes to this mint's HOLD balance.
# SY-USX and SY-eUSX are 1:1 with their respective underlying per the LP walker peg model.
# When a user posts a BuyYt limit order, their USX (or eUSX) goes into the orderbook's
# SY escrow — and per Exponent's flares spec, that USX still earns HOLD flares.
MINT_TO_S2_ORDERBOOKS = {
    USX_MINT:  ['A2yaEiehRCvibSdMWWJtrBdmVCYwGRNSNwg1VwdicthU'],   # USX-Sep26
    EUSX_MINT: ['3mXbVuMynj21doFXXEauJ2tGDV9kS2Q1SnnQDcgD54Bw'],   # eUSX-Sep26
}


def _xpbook_escrow_segments(wallet: str, mint: str, end_ts: int) -> list:
    """Return [(ts, cumulative_balance_token), ...] for this wallet's SY-escrow on
    the S2 orderbooks underlying `mint`. Cumulative running sum of signed deltas
    from xpbook_escrow_timeline. Empty list if no events.

    Used by build_twab_timeline as a virtual ATA — escrow balance is added to
    wallet ATA balance at each timestamp for TWAB integration.
    """
    orderbooks = MINT_TO_S2_ORDERBOOKS.get(mint)
    if not orderbooks:
        return []
    placeholders = ','.join('?' * len(orderbooks))
    try:
        con = db.conn()
        rows = con.execute(
            f"SELECT event_blocktime, delta_raw FROM xpbook_escrow_timeline "
            f"WHERE wallet = ? AND asset_kind = 'SY' "
            f"AND orderbook IN ({placeholders}) "
            f"AND event_blocktime BETWEEN ? AND ? "
            f"ORDER BY event_blocktime, rowid",
            [wallet] + list(orderbooks) + [S2_START_TS, end_ts]).fetchall()
    except Exception:
        return []   # table absent during initial migration — degrade gracefully
    if not rows:
        return []
    segs = [(S2_START_TS, 0.0)]
    cum = 0.0
    for r in rows:
        cum += r['delta_raw'] / 1e6   # raw → token units (USX/eUSX have 6 decimals)
        ts = r['event_blocktime']
        if ts == segs[-1][0]:
            segs[-1] = (ts, cum)
        else:
            segs.append((ts, cum))
    return segs


def _list_atas(wallet: str, mint: str):
    """Return list of ATAs, or None on RPC failure.

    The None return distinguishes 'wallet has no ATAs' (real empty) from
    'RPC retry-exhausted and returned {}' (silent failure). The old
    `r.get('result', {}).get('value', [])` conflated both — that's what
    cost 942 USX + 330 eUSX wallets their HOLD flares during refresh
    (cf. reference_walker_rpc_retry_fix)."""
    r = rpc('getTokenAccountsByOwner', [wallet, {'mint': mint}, {'encoding': 'jsonParsed'}], timeout=15)
    result = r.get('result')
    if result is None: return None
    return [a['pubkey'] for a in (result.get('value', []) or [])]


def _walk_ata_sigs(ata: str) -> list:
    snap = last_snapshot_ts()
    sigs = []; before = None
    for _ in range(10):
        params = [ata, {'limit': 1000, **({'before': before} if before else {})}]
        r = rpc('getSignaturesForAddress', params, timeout=20)
        page = r.get('result') or []
        if not page: break
        raw_batch_len = len(page)
        last_sig = page[-1]['signature']
        # Drop sigs newer than snapshot boundary.
        page = [s for s in page if (s.get('blockTime') or 0) <= snap]
        sigs.extend(page)
        if raw_batch_len < 1000: break
        before = last_sig
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
    """Return True if the cached HOLD entry is contradicted by wallet_quests.

    Two failure modes covered:
      A) atas:[] — RPC for getTokenAccountsByOwner returned empty during walk
      B) atas non-empty but timeline has max-balance == 0 — RPC for individual
         getTransaction calls all failed, leaving an all-zero balance trace
         even though the wallet earned flares.

    Both poison the 24h cache. We detect them by cross-checking against
    wallet_quests: if the wallet has positive flares for the DAILY quest,
    the cached timeline showing zero everywhere is structurally wrong.
    """
    if not cached: return False
    raw = cached.get('raw') or {}
    atas = raw.get('atas') or []
    timeline = raw.get('timeline') or []
    max_bal = max((float(b) for _, b in timeline), default=0.0) if timeline else 0.0
    # Cache is suspicious if: no ATAs found, OR timeline never showed any balance
    structurally_empty = (atas == []) or (max_bal == 0.0)
    if not structurally_empty: return False
    # Cross-check wallet_quests: do we have credit for this wallet?
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

    Returns: {'atas': [...], 'timeline': [[ts, balance_total], ...],
              'last_event_ts': int, 'fetch_failed': bool}

    Resilient to silent RPC failure: seeds the canonical ATA (deterministic
    from wallet+mint) and the prior cache's ATAs so a transient
    getTokenAccountsByOwner failure doesn't zero the wallet's HOLD timeline.
    `fetch_failed` flags the failure so callers can choose not to overwrite
    a known-good cache with a failed-RPC empty result."""
    fresh = _list_atas(wallet, mint)
    fetch_failed = (fresh is None)
    end_ts = min(last_snapshot_ts(), S2_END_TS)   # midnight-UTC cutoff

    canon = _canonical_ata(wallet, mint)
    prior_atas = []
    cache_key = _MINT_CACHE_KEY.get(mint)
    if cache_key:
        try:
            prior = db.get_cache(wallet, cache_key)
            prior_atas = ((prior or {}).get('raw') or {}).get('atas') or []
        except Exception:
            pass
    seed = list(prior_atas) + list(fresh or []) + ([canon] if canon else [])
    atas = list(dict.fromkeys(a for a in seed if a))

    if not atas:
        return {'atas': [], 'timeline': [[S2_START_TS, 0.0], [end_ts, 0.0]],
                'last_event_ts': end_ts, 'fetch_failed': fetch_failed}

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

    # XPBook escrow contribution — kept SEPARATE from the wallet ATA timeline so the
    # walker can apply the 7-day TVL maturity rule (per Solstice S2 docs) to escrow
    # only. Existing wallet-ATA HOLD continues to integrate linearly (matured wallets
    # dominate; the < 7-day fresh-deposit edge case is rare in practice).
    escrow_segs = _xpbook_escrow_segments(wallet, mint, end_ts)
    return {'atas': atas, 'timeline': timeline,
            'escrow_segs': escrow_segs, 'last_event_ts': end_ts,
            'fetch_failed': fetch_failed}


def integrate_daily(timeline: list, mult: int, usd_per_token, end_ts: int) -> float:
    """daily TWAB: balance × usd × mult × dt_days, with tail extension to end_ts.

    `usd_per_token` may be a constant (USX = 1.0) or a callable peg_fn(ts) → float
    (eUSX peg compounds smoothly; evaluated at segment midpoint for second-order
    accuracy). Using time-varying peg for eUSX is required — a constant peg
    over-credits early balances by ~0.5% per month on 5V9V."""
    flares = 0.0
    if not timeline: return 0.0
    is_callable = callable(usd_per_token)
    def _usd(t0, t1): return usd_per_token((t0 + t1) // 2) if is_callable else usd_per_token
    for i in range(len(timeline) - 1):
        t0, b0 = timeline[i]; t1, _ = timeline[i + 1]
        if t1 > end_ts: t1 = end_ts
        if t1 <= t0 or b0 <= 0: continue
        flares += b0 * _usd(t0, t1) * mult * (t1 - t0) / 86400.0
    last_t, last_b = timeline[-1]
    if last_t < end_ts and last_b > 0:
        flares += last_b * _usd(last_t, end_ts) * mult * (end_ts - last_t) / 86400.0
    return flares


def integrate_matured_daily(timeline: list, mult: float, usd_per_token,
                              end_ts: int, mature_days: int = 7) -> float:
    """Daily TWAB with N-day maturity floor.

    `usd_per_token` may be a constant float (USX=1.0) or a callable peg_fn(ts)→float
    (eUSX uses time-varying peg — matches integrate_daily semantics, evaluated at
    segment midpoint for second-order accuracy).
    """
    if not timeline or mature_days <= 0: return 0.0
    mature_sec = mature_days * 86400
    flares = 0.0
    is_callable = callable(usd_per_token)
    def _usd(t0, t1): return usd_per_token((t0 + t1) // 2) if is_callable else usd_per_token
    segments = []
    for i in range(len(timeline) - 1):
        t0, bal = timeline[i]; t1, _ = timeline[i + 1]
        if t1 > end_ts: t1 = end_ts
        if t1 > t0: segments.append((t0, bal, t1))
    last_t, last_b = timeline[-1]
    if last_t < end_ts: segments.append((last_t, last_b, end_ts))

    run_start = None
    for ts0, bal, ts1 in segments:
        if bal > 0:
            if run_start is None: run_start = ts0
            mature_ts = run_start + mature_sec
            earn_start = max(ts0, mature_ts)
            if earn_start < ts1:
                flares += bal * _usd(earn_start, ts1) * mult * (ts1 - earn_start) / 86400.0
        else:
            run_start = None
    return flares


def _balance_at(timeline: list, ts: int) -> float:
    """Last segment balance with t0 <= ts. Empty timeline → 0."""
    last = 0.0
    for t0, b in timeline:
        if t0 <= ts: last = b
        else: break
    return last


def _first_onchain_ts(timeline: list):
    """First timeline entry with ts > S2_START_TS — i.e. the first on-chain
    event observed inside the S2 window. The entry at S2_START_TS is the
    carry-in boundary marker (a synthetic state snapshot from pre-S2), not
    a qualifying tick."""
    for ts, _ in timeline:
        if ts > S2_START_TS: return ts
    return None


def integrate_qualified_bonus(timeline: list, min_bal: float, qualify_days: int,
                                 mult: int, usd_per_token, end_ts: int) -> float:
    """HOLD_*_1MO/3MO bonus = each completed `qualify_days`-long continuous-hold
    cycle pays `floor × mult` ONCE at the moment of completion.

    Per Solstice docs (users_flares_season-1.txt + S2 quest descriptions):
      "minimum X held for the whole period. Flares are rewarded at completion."

    Algorithm:
      1. Clock starts at the FIRST on-chain event in S2 (carry-in from pre-S2
         doesn't count as a qualifying tick — verified empirically against 5V9V).
      2. Sample balance at each 00:00 UTC boundary from clock-start onwards.
      3. While balance × usd_per_token ≥ min_bal: increment qrun, update
         cycle_floor = MIN(samples so far in this cycle).
      4. When qrun reaches qualify_days, credit cycle_floor × mult ONCE, reset
         qrun and cycle_floor, start the next cycle.
      5. Any sample below min_bal aborts the current cycle (no partial credit)
         and restarts the qualification clock.

    `usd_per_token` may be a constant (USX = 1.0) or a callable peg_fn(ts) → float
    (eUSX peg varies). Evaluated per 00:00 UTC sample.

    Verified on 5V9V: HOLD_USX_1MO walker = 1243.7990 vs Solstice 1243.80
    (99.9999% match — one completed cycle Apr 17–May 17 at floor 207.30 × 6×).
    Bug history: prior point-in-time integration over-credited 12.6× on 5V9V
    by accruing every second of post-qualification time × current balance,
    which exploded on brief-peak-then-dump patterns.
    """
    if min_bal <= 0 or qualify_days <= 0 or not timeline: return 0.0
    is_callable = callable(usd_per_token)
    def _usd(t): return usd_per_token(t) if is_callable else usd_per_token

    start_ts = _first_onchain_ts(timeline)
    if start_ts is None: return 0.0
    # Align to next 00:00 UTC at or after the first on-chain event.
    t = start_ts - (start_ts % 86400)
    if t < start_ts: t += 86400

    flares = 0.0
    qrun = 0
    cycle_floor = None
    while t <= end_ts:
        bal_usd = _balance_at(timeline, t) * _usd(t)
        if bal_usd >= min_bal:
            qrun += 1
            cycle_floor = bal_usd if cycle_floor is None else min(cycle_floor, bal_usd)
            if qrun == qualify_days:
                flares += cycle_floor * mult
                qrun = 0
                cycle_floor = None
        else:
            qrun = 0
            cycle_floor = None
        t += 86400
    return flares


def discover_universe_for_mint(mint: str) -> list:
    """Enumerate every owner of an SPL token account for `mint` across BOTH token
    programs. Ground truth: every wallet that has ever received this token has
    a token account.

    Retry-on-empty: known-active stablecoin mints (USX/eUSX) should never return
    0 holders. An empty result is an RPC flake — proceeding would zero out the
    entire HOLD universe in the downstream walker. Mirrors the protection in
    walk_s2_orca.py / walk_s2_kamino*.py.
    """
    import base64, base58, time as _t
    TOKEN_LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
    TOKEN_2022   = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
    owners = set()
    for prog, size in [(TOKEN_LEGACY, 165), (TOKEN_2022, None)]:
        filters = [{'memcmp': {'offset': 0, 'bytes': mint}}]
        if size: filters.insert(0, {'dataSize': size})
        accs = []
        for attempt in range(4):
            try:
                r = rpc('getProgramAccounts', [prog, {
                    'encoding': 'base64',
                    'dataSlice': {'offset': 32, 'length': 40},
                    'filters': filters,
                }], timeout=180, force_refresh=(attempt > 0))
                accs = r.get('result') or []
                if accs: break
            except Exception as e:
                print(f'  WARN discover {prog[:8]}.. {mint[:8]}.. attempt {attempt+1}: {e}', flush=True)
            _t.sleep(2 * (attempt + 1))
        if not accs:
            # Empty after 4 retries — token-2022 program legitimately empty for
            # mints not deployed there yet, so don't abort if the OTHER program
            # returned data. Only the per-program retry warning fires here.
            print(f'  WARN: {prog[:8]}.. {mint[:8]}.. empty after 4 retries', flush=True)
            continue
        for a in accs:
            d = base64.b64decode(a['account']['data'][0])
            if len(d) < 40: continue
            owner = base58.b58encode(d[:32]).decode()
            amount = int.from_bytes(d[32:40], 'little')
            if amount > 0: owners.add(owner)
    return sorted(owners)


def get_mint_supply(mint: str) -> float:
    """Current SPL mint supply (for cross-check)."""
    try:
        r = rpc('getAccountInfo', [mint, {'encoding': 'jsonParsed'}], timeout=10)
        info = r['result']['value']['data']['parsed']['info']
        return float(info['supply']) / (10 ** int(info['decimals']))
    except Exception: return 0.0
