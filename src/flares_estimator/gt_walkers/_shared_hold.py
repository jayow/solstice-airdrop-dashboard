"""Shared TWAB extraction for HOLD_USX_* and HOLD_EUSX_*.

Single extract → 3 transforms per mint (daily / 1MO / 3MO).
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))

from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
import db
from ._base import S2_START_TS, S2_END_TS, USX_MINT, EUSX_MINT


# Per-ATA inner pool for parallel `_post_balance` fetches inside the cold /
# incremental walks. Sized against the real global RPC semaphore (20 slots,
# rpc_helper._GLOBAL_CONCURRENCY default) and the HOLD outer pool of 16:
#   16 outer × 4 inner = 64 inner workers contending for 20 semaphore slots.
# The semaphore is the actual throughput gate; 4 inner per outer is the sweet
# spot — bigger inner pools just queue at the semaphore without extra reqs/sec
# and waste thread stacks. With ~600ms per _post_balance, a wallet with 12
# in-window sigs goes from ~7.2s serial to ~1.8s parallel (4× speedup) when
# the semaphore is contended; ~1.2s (6×) when it's not.
_POST_BALANCE_INNER_POOL = 4


def _parallel_post_balances(in_window_sigs, ata):
    """Fetch _post_balance for many sigs concurrently, preserve ascending-ts order.

    Returns a list of (ts, sig_str, bal) tuples sorted ascending by blockTime.
    `bal` is float on success (including the valid 0.0 close-event), None on
    failure (RPC returned no result, ata not in tx, OR exception escaped).

    Iteration / ordering:
      - We submit all futures up-front, keep an explicit ordered list of
        (future, sig_dict) tuples (NOT relying on dict insertion order — this
        is a Python 3.7+ guarantee but expressing it as a list makes the
        ascending-blockTime contract self-documenting).
      - After every future resolves, results are sorted by ts ascending using
        Python's stable sort — same-ts ties retain submission order, which is
        the original ascending-blockTime order from `_walk_ata_sigs` (line 145).
      - This matches serial semantics bit-for-bit: the caller's serial append
        loop sees the SAME (ts, sig, bal) sequence it would have built itself.

    Exception handling:
      - `_post_balance` is currently no-raise (rpc_helper returns dicts even
        on logical failure). We still wrap `fut.result()` in try/except so any
        future bug upstream (e.g. KeyError on a malformed tx) maps to bal=None
        → post_balance_failed=True, identical to the existing None branch.
      - This is a deliberate semantic widening: serial code would surface an
        uncaught exception as a walker crash; parallel code degrades to a
        flagged-failure ATA. We log the exception so silent-empty bugs don't
        hide (per feedback_systematic_troubleshooting).
    """
    if not in_window_sigs:
        return []
    pairs = []
    with ThreadPoolExecutor(max_workers=_POST_BALANCE_INNER_POOL,
                            thread_name_prefix='hold-postbal') as ex:
        # Submit in ascending-ts order; keep a list (not a dict) so the
        # ordering contract is explicit and survives any future refactor.
        submissions = [(ex.submit(_post_balance, s['signature'], ata), s)
                       for s in in_window_sigs]
        for fut, s in submissions:
            ts = s.get('blockTime') or 0
            try:
                bal = fut.result()
            except Exception as e:
                # See docstring — defensive widening. Print so silent failures
                # are diagnosable; do NOT re-raise (would orphan sibling sigs
                # in this ATA and tank the whole wallet walk).
                print(f'  WARN _post_balance raised for {ata[:8]}.. '
                      f'sig={s.get("signature","?")[:8]}..: {e}', flush=True)
                bal = None
            pairs.append((ts, s['signature'], bal))
    # Stable sort by ts ascending. With Python's Timsort this preserves the
    # original submission order for same-blockTime ties, matching the serial
    # iteration that `_walk_ata_sigs` produces.
    pairs.sort(key=lambda t: t[0])
    return pairs


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
    'RPC retry-exhausted and returned {}' (silent failure). Cf.
    reference_walker_rpc_retry_fix."""
    r = rpc('getTokenAccountsByOwner', [wallet, {'mint': mint}, {'encoding': 'jsonParsed'}], timeout=15)
    result = r.get('result')
    if result is None: return None
    return [a['pubkey'] for a in (result.get('value', []) or [])]


def _list_atas_with_balances(wallet: str, mint: str):
    """Same RPC as _list_atas but also extracts the current uiAmount per ATA.
    Returns {ata_pubkey: balance} or None on RPC failure. Used by the
    incremental fast-path to probe live balances with zero extra RPC cost."""
    r = rpc('getTokenAccountsByOwner', [wallet, {'mint': mint}, {'encoding': 'jsonParsed'}], timeout=15)
    result = r.get('result')
    if result is None: return None
    out = {}
    for acc in (result.get('value', []) or []):
        ata = acc.get('pubkey')
        info = (((acc.get('account') or {}).get('data') or {}).get('parsed') or {}).get('info') or {}
        amt = (info.get('tokenAmount') or {}).get('uiAmount')
        if ata: out[ata] = float(amt or 0)
    return out


def _walk_ata_sigs(ata: str, since_sig: str | None = None):
    """Page newest→oldest, stop at `since_sig` (incremental cursor) or after
    10 pages. Returns (sigs_sorted_ascending, rpc_failed).

    `since_sig` matches the walk_xpbook.py convention: stop when we encounter
    this signature going backwards — we get every sig newer than the cursor.
    On RPC failure (`result is None`), rpc_failed=True and the partial result
    so far is returned. Callers must NOT advance the cursor when rpc_failed."""
    snap = last_snapshot_ts()
    sigs = []; before = None; rpc_failed = False; hit_cursor = False
    for _ in range(10):
        params = [ata, {'limit': 1000, **({'before': before} if before else {})}]
        r = rpc('getSignaturesForAddress', params, timeout=20)
        if r.get('result') is None:
            rpc_failed = True
            break
        page = r.get('result') or []
        if not page: break
        raw_batch_len = len(page)
        # Walk the page newest→oldest looking for the cursor; collect everything
        # before the cursor. Sigs newer than snap are also dropped (boundary).
        for s in page:
            if since_sig and s.get('signature') == since_sig:
                hit_cursor = True
                break
            bt = s.get('blockTime') or 0
            if bt <= snap:
                sigs.append(s)
        if hit_cursor: break
        if raw_batch_len < 1000: break
        before = page[-1]['signature']
    sigs.sort(key=lambda s: s.get('blockTime') or 0)
    return sigs, rpc_failed


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
    if post:
        return float(post.get('uiTokenAmount', {}).get('uiAmount') or 0)
    # Account closed in this tx: post-balance missing, pre-balance present.
    # Record the zero-out so the timeline correctly captures the close.
    pre = next((b for b in (tx.get('meta', {}).get('preTokenBalances', []) or [])
                 if b.get('accountIndex') == idx), None)
    if pre:
        return 0.0
    return None


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


FORCE_FULL_WALK_INTERVAL = 7 * 86400          # 7 days
FAST_PATH_MIN_BALANCE_BUFFER = 110.0           # max(min_bal for 1MO/3MO=100) * 1.10
_BALANCE_EQ_EPS = 1e-9


def _cold_walk_ata(ata: str, end_ts: int):
    """Full sig-walk for one ATA, S2_START_TS through end_ts.

    Returns dict {segs, last_sig, last_sig_ts, last_known_balance,
    pre_s2_carry, rpc_failed, post_balance_failed}.
    `rpc_failed`: getSignaturesForAddress had a None result during pagination.
    `post_balance_failed`: at least one _post_balance call in S2-window returned
    None (treated as missing). Cursor advances only to the newest successfully
    decoded sig — so a failed sig won't be skipped on the next incremental pass.
    """
    sigs, rpc_failed = _walk_ata_sigs(ata)
    out = {'segs': [(S2_START_TS, 0.0)], 'last_sig': None, 'last_sig_ts': 0,
           'last_known_balance': 0.0, 'pre_s2_carry': 0.0,
           'rpc_failed': rpc_failed, 'post_balance_failed': False}
    if not sigs:
        # Still extend the synthetic end-of-window marker so downstream
        # integrators always see a final segment at end_ts.
        out['segs'].append((end_ts, 0.0))
        return out
    # Carry-in: the LAST pre-S2 sig's post-balance seeds the timeline at S2_START.
    # SERIAL — single RPC, not part of the per-sig loop (I8). Must stay outside
    # the parallel block: it writes to segs[0] which the in-window monotonicity
    # guard reads, so it has to land before the parallel fetch results.
    pre = [s for s in sigs if (s.get('blockTime') or 0) < S2_START_TS]
    if pre:
        r = _post_balance(pre[-1]['signature'], ata)
        if r is not None:
            out['pre_s2_carry'] = r
            out['segs'][0] = (S2_START_TS, r)
        else:
            out['post_balance_failed'] = True
    # PARALLEL fetch of in-window per-sig post-balances.
    # `_parallel_post_balances` returns (ts, sig, bal) tuples sorted ascending
    # by blockTime — identical ordering to the original serial iteration.
    in_window = [s for s in sigs
                 if S2_START_TS <= (s.get('blockTime') or 0) <= end_ts]
    results = _parallel_post_balances(in_window, ata)
    # SERIAL append: preserves I2 (monotonicity guard), I3 (newest_ok_sig is
    # the highest-ts successful sig — because results is ts-ascending, the
    # last successful assignment wins), I6 (OR-aggregate post_balance_failed).
    newest_ok_sig = None
    newest_ok_ts = 0
    for ts, sig_str, bal in results:
        if bal is None:
            out['post_balance_failed'] = True
            continue
        if ts <= out['segs'][-1][0]:
            continue
        out['segs'].append((ts, bal))
        newest_ok_sig = sig_str
        newest_ok_ts = ts
    if newest_ok_sig:
        out['last_sig'] = newest_ok_sig
        out['last_sig_ts'] = newest_ok_ts
        out['last_known_balance'] = out['segs'][-1][1]
    else:
        out['last_known_balance'] = out['segs'][0][1]
    return out


def _incremental_walk_ata(ata: str, since_sig: str, end_ts: int, prior_ata_state: dict):
    """Walk only sigs newer than `since_sig`, merging onto prior_ata_state's
    cached segs. Same return shape as _cold_walk_ata.
    Preserves I11 monotonicity (drops new sigs with ts <= last cached seg ts)."""
    sigs, rpc_failed = _walk_ata_sigs(ata, since_sig=since_sig)
    # Start from the prior cached state — we ONLY append.
    segs = [tuple(p) for p in prior_ata_state.get('segs') or [(S2_START_TS, 0.0)]]
    pre_s2_carry = float(prior_ata_state.get('pre_s2_carry') or 0.0)
    out = {'segs': segs, 'last_sig': prior_ata_state.get('last_sig'),
           'last_sig_ts': int(prior_ata_state.get('last_sig_ts') or 0),
           'last_known_balance': float(prior_ata_state.get('last_known_balance') or 0.0),
           'pre_s2_carry': pre_s2_carry,
           'rpc_failed': rpc_failed, 'post_balance_failed': False}
    if not sigs:
        return out
    # PARALLEL fetch of in-window per-sig post-balances.
    # No pre_s2_carry recompute here (I8) — incremental inherits the prior
    # value from prior_ata_state. Window filter matches the original loop:
    # ts < S2_START_TS or ts > end_ts is dropped.
    in_window = [s for s in sigs
                 if S2_START_TS <= (s.get('blockTime') or 0) <= end_ts]
    results = _parallel_post_balances(in_window, ata)
    # SERIAL append: preserves I2 (monotonicity vs cached segs[-1][0]), I3
    # (newest_ok_sig advances to the chronologically latest successful sig),
    # I6 (OR-aggregate post_balance_failed), I11 (no else branch — leave
    # last_known_balance at prior_ata_state's value when no new sig succeeded).
    newest_ok_sig = None
    newest_ok_ts = 0
    for ts, sig_str, bal in results:
        if bal is None:
            out['post_balance_failed'] = True
            continue
        if ts <= out['segs'][-1][0]:
            continue
        out['segs'].append((ts, bal))
        newest_ok_sig = sig_str
        newest_ok_ts = ts
    if newest_ok_sig:
        out['last_sig'] = newest_ok_sig
        out['last_sig_ts'] = newest_ok_ts
        out['last_known_balance'] = out['segs'][-1][1]
    return out


def build_twab_timeline(wallet: str, mint: str) -> dict:
    """Build a unified balance timeline across every ATA owned by `wallet`.

    Returns: {'atas', 'timeline', 'escrow_segs', 'last_event_ts',
              'fetch_failed', 'per_ata', '_schema'}

    Incremental cache (schema-2): per-ATA state stored as
        per_ata: {ata: {segs, last_sig, last_sig_ts, last_known_balance,
                        pre_s2_carry, last_full_walk_ts, closed}}
    Three paths per ATA:
      COLD — no schema-2 cache OR last_full_walk_ts > 7d ago. Full sig walk.
      FAST — live balance matches cached.last_known_balance within ε, AND
             live_bal > FAST_PATH_MIN_BALANCE_BUFFER, AND cache < 7d old.
             Reuse cached segs verbatim, no sig walk.
      SLOW — schema-2 cache present but FAST gate failed. Walk only sigs newer
             than cached.last_sig, append to cached segs.

    Risk shapes:
      - Round-trip wash (A → B → A within the cycle): FAST path would miss the
        dip; FAST_PATH_MIN_BALANCE_BUFFER gates it out for balances near min_bal.
        7-day forced cold walk bounds the worst-case area drift.
      - Closed ATA: not present in live result. Kept in cache; we trust its
        prior segs and skip future walking.
      - RPC failure: fetch_failed=True propagates to caller, which must NOT
        overwrite a known-good cache with a failed-RPC result.
    """
    end_ts = min(last_snapshot_ts(), S2_END_TS)
    now_ts = int(time.time())
    cache_key = _MINT_CACHE_KEY.get(mint)
    canon = _canonical_ata(wallet, mint)

    # 1. Load prior schema-2 (or schema-1 legacy) cache.
    prior_raw = {}
    prior_per_ata = {}
    schema = 1
    if cache_key:
        try:
            prior = db.get_cache(wallet, cache_key)
            prior_raw = (prior or {}).get('raw') or {}
            prior_per_ata = dict(prior_raw.get('per_ata') or {})
            schema = int(prior_raw.get('_schema') or 1)
        except Exception:
            pass
    prior_atas = list(prior_raw.get('atas') or [])

    # 2. Probe live state: one RPC gets ATAs + balances. None = RPC failure.
    live = _list_atas_with_balances(wallet, mint)
    fetch_failed = (live is None)
    live = live if live is not None else {}

    # 3. Seed the working ATA set.
    seed = list(prior_atas) + list(live.keys()) + ([canon] if canon else [])
    atas = list(dict.fromkeys(a for a in seed if a))

    if not atas:
        return {'atas': [], 'timeline': [[S2_START_TS, 0.0], [end_ts, 0.0]],
                'escrow_segs': _xpbook_escrow_segments(wallet, mint, end_ts),
                'last_event_ts': end_ts, 'fetch_failed': fetch_failed,
                'per_ata': {}, '_schema': 2}

    # 3.5. SCHEMA-1 MIGRATION FAST PATH (whole-wallet check).
    # Legacy caches store the summed timeline across all ATAs (no per-ATA split).
    # If the live state matches the cache's recorded end-state — same ATA set
    # AND same total balance — the existing timeline is authoritative; no walk
    # needed. Carries the legacy timeline into a schema-2 per_ata shape so the
    # next refresh hits the schema-2 FAST path proper.
    #
    # We synthesize per_ata by putting the entire summed timeline on the FIRST
    # ATA and zero-segs on the rest. The union-fold below sums them back to the
    # correct total. This is exact for the dominant single-ATA wallet case and
    # produces the correct WALLET-level integrand for multi-ATA (the per-ATA
    # split is lost in schema-1 anyway, but downstream integrate_* only sees
    # the wallet-level timeline).
    if (schema == 1 and not fetch_failed and prior_raw.get('timeline')
            and prior_atas and live):
        legacy_timeline = prior_raw['timeline']
        # Migration only trusts FLAT timelines (≤2 points = wallet never moved
        # within S2). Active wallets (3+ points) get COLD walked because some
        # schema-1 caches contain spurious mid-window balances from old RPC
        # bugs that would silently propagate via migration.
        is_passive = len(legacy_timeline) <= 2
        cached_last_balance = float(legacy_timeline[-1][1])
        live_total_balance = sum(live.values())
        same_atas = set(prior_atas) == set(live.keys())
        same_bal = abs(live_total_balance - cached_last_balance) < _BALANCE_EQ_EPS
        if is_passive and same_atas and same_bal:
            new_per_ata = {}
            carrier = atas[0]
            for ata in atas:
                if ata == carrier:
                    segs = [(int(t), float(b)) for t, b in legacy_timeline]
                    new_per_ata[ata] = {
                        'segs': segs,
                        'last_sig': None,
                        'last_sig_ts': int(segs[-1][0]) if segs else 0,
                        'last_known_balance': cached_last_balance,
                        'pre_s2_carry': float(segs[0][1]) if segs else 0.0,
                        'last_full_walk_ts': 0,   # forces COLD next refresh; cheap because state is stable
                        'closed': False,
                    }
                else:
                    new_per_ata[ata] = {
                        'segs': [(S2_START_TS, 0.0), (end_ts, 0.0)],
                        'last_sig': None, 'last_sig_ts': 0,
                        'last_known_balance': 0.0, 'pre_s2_carry': 0.0,
                        'last_full_walk_ts': 0, 'closed': False,
                    }
            # Union-fold back to wallet-level timeline (same logic as main path).
            all_ts = sorted({S2_START_TS, end_ts} |
                            {ts for st in new_per_ata.values() for ts, _ in st['segs']})
            timeline = []
            for t in all_ts:
                total = 0.0
                for st in new_per_ata.values():
                    last = 0.0
                    for ts, b in st['segs']:
                        if ts <= t: last = b
                        else: break
                    total += last
                if not timeline or total != timeline[-1][1] or t == end_ts:
                    timeline.append([t, total])
            return {
                'atas': list(new_per_ata.keys()), 'timeline': timeline,
                'escrow_segs': _xpbook_escrow_segments(wallet, mint, end_ts),
                'last_event_ts': end_ts, 'fetch_failed': False,
                'per_ata': new_per_ata, '_schema': 2,
            }

    # 4. Per-ATA decision: COLD / FAST / SLOW. Track any per-ata RPC issues.
    new_per_ata = {}
    any_rpc_failed = False
    any_post_failed = False
    for ata in atas:
        ps = prior_per_ata.get(ata)
        ps_schema_ok = (schema == 2) and isinstance(ps, dict) and (ps.get('last_sig') is not None)
        live_bal = live.get(ata)
        in_live = (ata in live)
        last_full_walk = int((ps or {}).get('last_full_walk_ts') or 0)
        cache_age_ok = ps_schema_ok and ((now_ts - last_full_walk) < FORCE_FULL_WALK_INTERVAL)

        # FAST path: live balance matches cached, above buffer, cache < 7d.
        if (cache_age_ok and in_live and live_bal is not None and ps is not None and
                abs(live_bal - float(ps.get('last_known_balance') or 0.0)) < _BALANCE_EQ_EPS and
                live_bal > FAST_PATH_MIN_BALANCE_BUFFER):
            new_per_ata[ata] = {
                'segs': [list(p) for p in (ps.get('segs') or [(S2_START_TS, 0.0)])],
                'last_sig': ps.get('last_sig'),
                'last_sig_ts': int(ps.get('last_sig_ts') or 0),
                'last_known_balance': float(ps.get('last_known_balance') or 0.0),
                'pre_s2_carry': float(ps.get('pre_s2_carry') or 0.0),
                'last_full_walk_ts': last_full_walk,
                'closed': False,
            }
            continue

        # SLOW path: schema-2 cache with cursor present; incremental walk.
        if ps_schema_ok and not fetch_failed:
            res = _incremental_walk_ata(ata, ps['last_sig'], end_ts, ps)
            any_rpc_failed = any_rpc_failed or res['rpc_failed']
            any_post_failed = any_post_failed or res['post_balance_failed']
            # If RPC failed, don't advance cursor — keep prior values verbatim.
            if res['rpc_failed']:
                new_per_ata[ata] = dict(ps)
                continue
            new_per_ata[ata] = {
                'segs': [list(p) for p in res['segs']],
                'last_sig': res['last_sig'],
                'last_sig_ts': res['last_sig_ts'],
                'last_known_balance': res['last_known_balance'],
                'pre_s2_carry': res['pre_s2_carry'],
                'last_full_walk_ts': last_full_walk,   # FAST/SLOW don't reset 7d clock
                'closed': (not in_live) and (live is not None),
            }
            continue

        # COLD path: full walk.
        res = _cold_walk_ata(ata, end_ts)
        any_rpc_failed = any_rpc_failed or res['rpc_failed']
        any_post_failed = any_post_failed or res['post_balance_failed']
        new_per_ata[ata] = {
            'segs': [list(p) for p in res['segs']],
            'last_sig': res['last_sig'],
            'last_sig_ts': res['last_sig_ts'],
            'last_known_balance': res['last_known_balance'],
            'pre_s2_carry': res['pre_s2_carry'],
            'last_full_walk_ts': now_ts,
            'closed': (not in_live) and (not res['rpc_failed']) and (not fetch_failed),
        }

    # 5. Build the union timeline from new_per_ata segs (same fold as v1).
    all_ts = sorted({S2_START_TS, end_ts} |
                    {ts for st in new_per_ata.values() for ts, _ in st.get('segs') or []})
    timeline = []
    for t in all_ts:
        total = 0.0
        for st in new_per_ata.values():
            last = 0.0
            for ts, b in (st.get('segs') or []):
                if ts <= t: last = b
                else: break
            total += last
        if not timeline or total != timeline[-1][1] or t == end_ts:
            timeline.append([t, total])

    # 6. Escrow segs are ALWAYS read fresh from xpbook_escrow_timeline.
    escrow_segs = _xpbook_escrow_segments(wallet, mint, end_ts)

    return {
        'atas': list(new_per_ata.keys()),
        'timeline': timeline,
        'escrow_segs': escrow_segs,
        'last_event_ts': end_ts,
        'fetch_failed': fetch_failed or any_rpc_failed,
        'per_ata': new_per_ata,
        '_schema': 2,
    }


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
