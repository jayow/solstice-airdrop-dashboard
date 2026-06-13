"""Walk S2 LP holders across both Exponent markets (USX-Jun26, eUSX-Jun26).

For each LP-vault sig in [S2_START_TS, now]:
  - Parse tx to get user wallet + LP delta + underlying delta
  - Append to per-wallet timeline

Then integrate LP_balance × per_LP_USD × multiplier over time for each wallet.

Formula (verified at 100-101% on user's S1 data):
  per_LP_USD(t) = |underlying_delta_at_tx| / |LP_delta_at_tx| × peg
  daily_flares = LP_balance × per_LP_USD × mult

Output: data/s2_lp_flares.json  {wallet: {USX: flares, eUSX: flares}}
"""
import os, sys, json, time, math, base64, base58, struct
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
import walker_db

# Exponent program IDs — emit the Wrapper events via emit_cpi.
# V1 core deployed since launch; V2 wrapper launched ~2026-05 with a parallel
# set of markets (e.g. GesxMwfk... = USX V2). V2 emits the same event
# discriminators as V1 (WrapperProvideLiquidity etc.) with the same first
# few u64 fields (base_in, lp_out, yt_out, ...), but longer total payload
# (280 bytes vs V1's ~112) — the trailing fields are V2-specific context
# that our decoder ignores. user@[16:48], market@[48:80], lp_field@[80+i*8].
EXPONENT_PROG_V1 = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
EXPONENT_PROG_V2 = 'XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC'
EXPONENT_PROGS = {EXPONENT_PROG_V1, EXPONENT_PROG_V2}
# Backwards-compat alias for any code reading the old constant name.
EXPONENT_PROG = EXPONENT_PROG_V1

# Anchor event discriminators for each Wrapper LP event. Computed as
# sha256("event:<EventName>")[:8]; verified against exponent-core source.
#
# V1 event layout:
#   bytes[0:8]   anchor IX disc (CPI sentinel, ignored)
#   bytes[8:16]  event disc
#   bytes[16:48] user pubkey
#   bytes[48:80] market pubkey
#   ... event-specific u64 fields ...
#   bytes[-8:]   lp_price (f64) — canonical lp_price_in_asset() at tx time
# Each entry: (event_type, lp_field_idx, sign, base_field_idx).
# lp_field_idx = 0-indexed u64 position of lp_in/lp_out within the body.
# base_field_idx = position of base_in/base_out (used to derive lp_price for V2).
_LP_EVENT_TYPES_V1 = {
    bytes.fromhex('d12ae34dbbd811b1'): ('provide',         1, +1, 0),  # base_in, lp_out, yt_out, lp_price
    bytes.fromhex('3c79a45ddc0d8ec5'): ('provide_base',    3, +1, 0),  # base_in, pt_out, sy_in, lp_out, lp_price
    bytes.fromhex('57a396a2ba93eac8'): ('provide_classic', 2, +1, 0),  # base_in, pt_in, lp_out, lp_price
    bytes.fromhex('3420b4f124dd48a7'): ('withdraw',         1, -1, 0), # base_out, lp_in, lp_price
    bytes.fromhex('129ad42724179e7c'): ('withdraw_classic', 1, -1, 0), # base_out, lp_in, pt_out, lp_price
}

# V2 wrapper (Exponent CLMM) event layout — IDL-confirmed via
# @exponent-labs/exponent-clmm-idl, cross-validated on 20 V2 events
# across 3 control wallets (5V9V, GPQs, 7VsV9DUW). Offsets are BYTES into
# body (= data[16:], so user@[0:32], market@[32:64]).
# Each entry: (event_type, lp_off, sy_off, lp_sign, sy_sign)
#   lp_off  — u64 LE byte offset for the LP amount (lp_out for provides,
#             lp_in for withdraws). 1e6 scaling, all markets are 6 dec.
#   sy_off  — u64 LE byte offset for the SY-side flow:
#             provide_base → sy_to_pool, provide → sy_in, withdraw* → sy_received
_LP_EVENT_TYPES_V2 = {
    bytes.fromhex('d12ae34dbbd811b1'): ('provide',          72,  80, +1, +1),
    bytes.fromhex('3c79a45ddc0d8ec5'): ('provide_base',    120, 136, +1, +1),
    bytes.fromhex('57a396a2ba93eac8'): ('provide_classic',  72,  80, +1, +1),
    bytes.fromhex('3420b4f124dd48a7'): ('withdraw',         72,  88, -1, -1),
    bytes.fromhex('129ad42724179e7c'): ('withdraw_classic', 72,  88, -1, -1),
}

# Offset of `amount_base_in` within the event body (data[16:]) for provide_base.
# For 'provide' events the user supplies PT+SY directly (no base swap), so
# base_in does not exist — fall back to sy_in. 'provide_classic' has a
# different payload layout than provide_base; its base offset is not yet
# reverse-engineered, so it falls back to sy_in.
# Derived 2026-06-07 from GPQs USX-Sep26 control: payload[96:104] = 1009.57 USX,
# matches Solstice target 43,452 at 99.7% via base_in × peg × mult × dt.
_LP_BASE_IN_OFFSETS_V2 = {
    bytes.fromhex('3c79a45ddc0d8ec5'):  96,  # provide_base
}

# Boost-end for Sep26 launch markets (unix). Per Solstice API multiplier
# history across all snapshots: Sep26 LP/YT multipliers were elevated 1.5×
# (LP 30/15, YT 45/22.5) from market open (2026-05-18) until this timestamp,
# then reverted to base (LP 20/10, YT 30/15). Cross-verified with YT walker
# multi-wallet match (memory project_solstice_50pct_boost_window).
BOOST_END_TS = 1780012800  # 2026-05-29 00:00 UTC

# Backwards-compat alias (V1 fields, 3-tuple form) for any external caller.
_LP_EVENT_TYPES = {disc: (name, idx, sign) for disc, (name, idx, sign, _) in _LP_EVENT_TYPES_V1.items()}


def _decode_lp_event(data: bytes, version: str = 'v1'):
    """Decode an Exponent Wrapper LP event from emit_cpi inner-ix data.

    V1 returns (event_type, user, market, lp_raw, lp_price, sign).
    V2 returns (event_type, user, market, lp_raw, sy_raw, lp_sign, sy_sign).

    V1: lp_price = trailing f64 in payload (canonical lp_price_in_asset()).
    V2: lp_price isn't directly emitted — we surface sy_to_pool/sy_in/sy_received
        instead, which is what the integration formula uses
        (sy_value × peg × mult × dt — Solstice's V2 LP attribution).
    """
    if len(data) < 16 + 32 + 32: return None
    disc = data[8:16]
    user = base58.b58encode(data[16:48]).decode()
    market = base58.b58encode(data[48:80]).decode()

    if version == 'v2':
        if disc not in _LP_EVENT_TYPES_V2: return None
        event_type, lp_off, sy_off, lp_sign, sy_sign = _LP_EVENT_TYPES_V2[disc]
        body = data[16:]   # user@[0:32], market@[32:64]
        if lp_off + 8 > len(body) or sy_off + 8 > len(body): return None
        lp_raw = int.from_bytes(body[lp_off:lp_off+8], 'little')
        sy_raw = int.from_bytes(body[sy_off:sy_off+8], 'little')
        base_off = _LP_BASE_IN_OFFSETS_V2.get(disc)
        base_raw = int.from_bytes(body[base_off:base_off+8], 'little') if base_off and base_off + 8 <= len(body) else 0
        return event_type, user, market, lp_raw, sy_raw, lp_sign, sy_sign, base_raw

    if disc not in _LP_EVENT_TYPES_V1: return None
    event_type, lp_field_idx, sign, _ = _LP_EVENT_TYPES_V1[disc]
    body = data[80:-8]
    n_u64 = len(body) // 8
    if lp_field_idx >= n_u64: return None
    lp_raw = int.from_bytes(body[lp_field_idx*8:lp_field_idx*8+8], 'little')
    lp_price = struct.unpack('<d', data[-8:])[0]
    # Sanity-bound lp_price: it's read as a trailing f64, and a byte-misaligned
    # decode yields garbage (NaN / inf / huge / negative). Real Exponent LP
    # prices are ~O(1). Integration does lp_balance × sqrt(lp_price) × mult × dt,
    # so an unbounded lp_price is the tens-of-billions blow-up class (cf. the V2
    # withdraw decode bug). The `not (0 < x < 100)` test rejects NaN/inf/≤0/huge
    # in one shot — drop the event rather than emit a blow-up.
    if not (0 < lp_price < 100):
        return None
    return event_type, user, market, lp_raw, lp_price, sign


def _extract_inner_ix_data(tx: dict) -> list:
    """Return [(programId, data_bytes), ...] for every inner instruction in the tx.
    Handles both jsonParsed (base58 data) and base64 encoding formats."""
    out = []
    meta = tx.get('meta') or {}
    inner = meta.get('innerInstructions') or []
    for group in inner:
        for ix in (group.get('instructions') or []):
            pid = ix.get('programId') or ix.get('program')
            d = ix.get('data')
            if not pid or not d: continue
            try:
                # jsonParsed default encoding is base58 for unknown programs
                raw = base58.b58decode(d)
            except Exception:
                try: raw = base64.b64decode(d)
                except Exception: continue
            out.append((pid, raw))
    return out

S2_START_TS = 1776038400
S2_END_TS   = 1785024000   # only used to cap if walking beyond now

# Live eUSX/USD price — pulled from Solstice's protocol API (canonical source).
# We previously read offset 48 of an on-chain PDA which returned ~1.156, but
# that turns out to be a different vault ratio, NOT the eUSX→USD price. The
# correct number (~1.032 as of May 2026) lives in Solstice's /api/protocol
# `eusxPrice` and Exponent's market `syExchangeRate`.
import struct
def get_eusx_peg():
    import urllib3 as _u
    _u.disable_warnings()
    try:
        r = requests.get('https://app.solstice.finance/api/protocol', timeout=8, verify=False).json()
        p = float(r.get('eusxPrice') or 0)
        if 0.9 < p < 2.0: return p
    except Exception: pass
    return 1.0319   # fallback

MARKETS = {
    'USX-Jun26': {
        'market':       'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm',
        'lp_vault':     'CRPy147RiyosYzEzU4NLVbhZjhy1GGAHtXyvbfFHK736',
        'lp_mint':      'BR2JKV9gPoJfX8A8DkFmo2yNQKCeGipg33oYaZ4EmjbW',
        'underlying':   '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',
        'mult':         20,
        'peg':          1.0,
        'quest':        'S2_EXPONENT_LP_USX_JUN26',
    },
    'eUSX-Jun26': {
        'market':       'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP',
        'lp_vault':     '3wMTeNVRXsYaMuVMpX6uAXVdX2zi5puk7SbsU5YXts82',
        'lp_mint':      '4GT6g1iKx2TyYCkwt1tERkReQjSUuVE7uh14M5W8v2nn',
        'underlying':   '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
        'mult':         10,
        'peg':          None,   # populated from chain
        'quest':        'S2_EXPONENT_LP_EUSX_JUN26',
    },
    # V2 Sep16 2026 maturity markets (launched 2026-05-18, 1.5× boost vs Jun01).
    # V2 emits the same WrapperProvideLiquidity / WrapperWithdrawLiquidity events
    # as V1 with identical 96-byte payload — no parser changes needed.
    # market == sig-walk anchor (vault PDA == market PDA in V2).
    # lp_vault is the ATA the market PDA owns that holds all LP tokens (from
    # getTokenLargestAccounts on the LP mint, 2026-05-21).
    'USX-Sep26': {
        'market':       '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm',
        'lp_vault':     '83ayni8GmBwXwM4LXTEzgVV6sLZSYNJMT2JiFzAaQ2KJ',
        'lp_mint':      'UPvchSL3aVFwaqVEQKJ3msSLfhKJe468DBPJYgWLNw5',
        'underlying':   '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',
        # Empirically (5V9V calibration 2026-06-06): Solstice's +50% launch
        # boost applied to YT only, NOT to LP. With boost the walker was
        # 208% over Solstice on 5V9V's USX-Sep26 LP; with base-only mult and
        # sqrt(rate) valuation, walker matches at 98.22%.
        'mult':         20,
        'peg':          1.0,
        'quest':        'S2_EXPONENT_LP_USX_SEP26',
    },
    'eUSX-Sep26': {
        'market':       'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf',
        'lp_vault':     'jNP3KbSyrE6hrjNsShmX2q4LBZTpvDLkudedSmdDVGY',
        'lp_mint':      '72AitxuVjN9LF88jMEMKX6NRqTufeJ9XRdtkj96UuKT2',
        'underlying':   '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
        # Same as USX-Sep26: launch boost was YT-only; LP is flat mult.
        'mult':         10,
        'peg':          None,   # populated from chain
        'quest':        'S2_EXPONENT_LP_EUSX_SEP26',
    },
    # V2 wrapper (Exponent CLMM) Sep26 launch markets — re-enabled 2026-06-07
    # with IDL-confirmed decoder + sy_to_pool × peg × mult × dt integration.
    # Sig-walk anchor is the MARKET PDA itself (writable on every LP op);
    # there's no lp_vault to walk because the wrapper PDA owns LP per-user.
    # Events are filtered by user==wallet (wrapper relay txs only credit
    # actual makers, not the wrapper itself).
    # Both V1 and V2 contribute to the SAME quest code (USX_SEP26 / EUSX_SEP26)
    # — see project_v2_clmm_lp_walker_2026_06_06.md.
    'USX-V2-Sep26': {
        'market':       'GesxMwfknVkVziqJKfGrNyhSoeBxdSEE6ZqpXk9Kbci8',
        'sig_anchor':   'GesxMwfknVkVziqJKfGrNyhSoeBxdSEE6ZqpXk9Kbci8',
        # Solstice API multiplier history showed 30× pre-boost (until
        # 1780012800 = 5/29 00:00 UTC), 20× post-boost. We apply per-event
        # mult via mult_at(quest, ts) during integration so positions held
        # during the boost window get credited at 30×.
        'mult':         20,
        'boost_mult':   30,
        'boost_end_ts': BOOST_END_TS,
        'peg':          1.0,
        'quest':        'S2_EXPONENT_LP_USX_SEP26',
        'version':      'v2',
    },
    'eUSX-V2-Sep26': {
        'market':       '4yf98Xwht4z2K86VCHaS8tt5cbAxG529R7xVUs2dUf64',
        'sig_anchor':   '4yf98Xwht4z2K86VCHaS8tt5cbAxG529R7xVUs2dUf64',
        'mult':         10,
        'boost_mult':   15,
        'boost_end_ts': BOOST_END_TS,
        'peg':          None,   # populated from chain (live eUSX peg)
        'quest':        'S2_EXPONENT_LP_EUSX_SEP26',
        'version':      'v2',
    },
}


def _mult_at(quest_code: str, ts: int, cfg: dict) -> float:
    """Per-event multiplier honouring the API boost window.

    Reads cfg.boost_mult / cfg.boost_end_ts (set per V2 market) and returns
    boost_mult if ts < boost_end_ts, else base mult. V1 markets don't set
    boost_mult so they always return the base.
    """
    boost_end = cfg.get('boost_end_ts') or 0
    boost_mult = cfg.get('boost_mult')
    if boost_end and boost_mult and ts < boost_end:
        return float(boost_mult)
    return float(cfg.get('mult', 0))


def fetch_all_sigs(addr: str, until_ts: int = 0) -> list:
    """Pull sigs newest→oldest. If until_ts > 0, stop once a sig is older than it;
    if until_ts == 0, walk the full history (needed for pre-S2 carry-in).

    ALSO filters out sigs newer than last_snapshot_ts() — those flares aren't
    counted by the integration step, so fetching their tx details is pure
    RPC waste.

    CRITICAL: an empty result mid-pagination is NOT a reliable end-of-history
    marker — RPC nodes (especially from CI runners) sometimes return [] under
    load even when more sigs exist. Retry 4× before treating empty as terminal.
    """
    import time as _t
    snap = last_snapshot_ts()
    sigs = []
    before = None
    while True:
        batch = []
        for attempt in range(4):
            params = [addr, {'limit': 1000}]
            if before: params[1]['before'] = before
            r = rpc('getSignaturesForAddress', params, force_refresh=(attempt > 0))
            batch = r.get('result', []) or []
            if batch: break
            _t.sleep(0.5 * (attempt + 1))
        if not batch: break
        raw_batch_len = len(batch)
        last_sig = batch[-1]['signature']
        # Filter: only keep sigs from before the snapshot boundary.
        # Pagination uses raw_batch_len (not filtered), so intraday tails don't
        # falsely terminate the walk.
        batch = [s for s in batch if (s.get('blockTime') or 0) <= snap]
        if until_ts > 0:
            keep = [s for s in batch if (s.get('blockTime') or 0) >= until_ts]
            sigs.extend(keep)
            if len(keep) < len(batch): break
        else:
            sigs.extend(batch)
        if raw_batch_len < 1000: break
        before = last_sig
    return sigs


def _tx_signer(tx: dict) -> str:
    """Return the fee-payer / primary signer of the tx. For every Exponent
    Wrapper* instruction this is the end user — Exponent doesn't have a relayer
    or meta-tx flow. Authoritative attribution; no heuristics needed."""
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    # jsonParsed format: list of {pubkey, signer, writable, source}
    for k in keys:
        if isinstance(k, dict) and k.get('signer'):
            return k.get('pubkey')
        if isinstance(k, str):
            # Older raw format: signers are the first N entries per
            # header.numRequiredSignatures. Fee payer is index 0.
            return k
    return None


def parse_tx_lp_event(tx: dict, cfg: dict) -> dict:
    """Parse an Exponent LP tx using the program's emitted Wrapper event.

    Returns the canonical (user, lp_delta, lp_price, underlying_delta) where
    lp_price is the exact value Exponent's dashboard displays — computed by
    `market.financials.lp_price_in_asset()` and emitted via emit_cpi! at the
    moment of the tx. This bypasses heuristics entirely:

      * No <1M USD balance filter (worked but skipped whales)
      * No PT/underlying delta accounting (works but loses to slippage)
      * No eUSX peg multiplication (mismatched Exponent's accounting)

    The dashboard shows `amount_lp × lp_price` (in market asset units). For
    flare math, integrate `lp_balance × lp_price(t) × mult × dt` — no peg.
    """
    meta = tx.get('meta') or {}
    if meta.get('err'): return None

    market_pk = cfg['market']

    # Outer signers of the tx — used to detect CPI-routed LP activity. When
    # another protocol (e.g. Loopscale) deposits into Exponent on a user's
    # behalf, the event's `user_address` is the protocol's PDA (PDA-style
    # signed via Anchor seeds), but the outer tx is signed by the actual
    # user. The real user gets rewarded via THEIR primary quest
    # (S2_LOOPSCALE_SUPPLY etc.) — crediting them here with S2_EXPONENT_LP
    # would double-count. The PDA itself should be excluded from leaderboards
    # since it doesn't represent a real participant.
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    outer_signers = {k.get('pubkey') for k in keys if isinstance(k, dict) and k.get('signer')}

    # Scan inner instructions for an Exponent Wrapper LP event matching this market.
    for prog, data in _extract_inner_ix_data(tx):
        if prog not in EXPONENT_PROGS: continue
        version = 'v2' if prog == EXPONENT_PROG_V2 else 'v1'
        decoded = _decode_lp_event(data, version=version)
        if not decoded: continue
        event_type, user, evt_market, lp_raw, lp_price, sign = decoded
        if evt_market != market_pk: continue
        # CPI-routed depositor → skip. event's user_address must be in the
        # outer tx's signers list for a direct, user-initiated LP action.
        if user not in outer_signers:
            return None
        # Convert lp_raw to UI amount.
        lp_decimals = _get_lp_decimals(cfg['lp_mint'])
        lp_amount_ui = lp_raw / (10 ** lp_decimals)
        lp_delta = sign * lp_amount_ui  # + on supply, - on withdraw

        # Also extract signer-side underlying/PT deltas for snapshot/UI context.
        signer = user  # event user_address == outer signer (verified above)
        pre = {(t['accountIndex'], t['mint']): t for t in (meta.get('preTokenBalances') or [])}
        post = {(t['accountIndex'], t['mint']): t for t in (meta.get('postTokenBalances') or [])}
        pt_mint = _get_pt_mint(market_pk)
        underlying_delta = 0.0
        pt_delta = 0.0
        for k in set(pre) | set(post):
            _, mint = k
            pb = pre.get(k); pob = post.get(k)
            owner = (pob or pb).get('owner')
            if owner != signer: continue
            pre_ui = float((pb or {}).get('uiTokenAmount', {}).get('uiAmount') or 0)
            post_ui = float((pob or {}).get('uiTokenAmount', {}).get('uiAmount') or 0)
            d = post_ui - pre_ui
            if abs(d) < 1e-12: continue
            if mint == cfg['underlying']: underlying_delta += d
            elif pt_mint and mint == pt_mint: pt_delta += d

        return {
            'user': user,
            'event_type': event_type,
            'lp_delta': lp_delta,
            'lp_price': lp_price,           # canonical — what Exponent dashboard shows
            'underlying_delta': underlying_delta,  # informational only
            'pt_delta': pt_delta,                  # informational only
            'per_lp_underlying': lp_price,         # kept for back-compat with integration loop
        }
    return None


# Cache of LP mint → decimals. LP mints are usually 6-decimal but we read it
# once per mint to be safe; market_two.rs doesn't hardcode this.
_lp_decimals_cache: dict = {}

def _get_lp_decimals(lp_mint: str) -> int:
    if lp_mint in _lp_decimals_cache: return _lp_decimals_cache[lp_mint]
    r = rpc('getAccountInfo', [lp_mint, {'encoding': 'jsonParsed'}])
    v = r.get('result', {}).get('value')
    decs = 6
    if v:
        info = (v.get('data', {}) or {}).get('parsed', {}).get('info', {}) or {}
        decs = int(info.get('decimals') or 6)
    _lp_decimals_cache[lp_mint] = decs
    return decs


# Cache of market PDA → its offset-40 mint (PT). Avoids re-fetching the market
# account per tx (it's static for the lifetime of the market).
_pt_mint_cache: dict = {}

def _get_pt_mint(market_pk: str):
    if market_pk in _pt_mint_cache: return _pt_mint_cache[market_pk]
    r = rpc('getAccountInfo', [market_pk, {'encoding': 'base64'}])
    v = r.get('result', {}).get('value')
    if not v:
        _pt_mint_cache[market_pk] = None; return None
    d = base64.b64decode(v['data'][0])
    if len(d) < 72:
        _pt_mint_cache[market_pk] = None; return None
    mint = base58.b58encode(d[40:72]).decode()
    _pt_mint_cache[market_pk] = mint
    return mint


def main():
    eusx_peg = get_eusx_peg()
    MARKETS['eUSX-Jun26']['peg'] = eusx_peg
    MARKETS['eUSX-Sep26']['peg'] = eusx_peg
    print(f'eUSX live peg: ${eusx_peg:.6f}', flush=True)

    now_ts = last_snapshot_ts()   # midnight-UTC cutoff (Solstice snapshot cadence)
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → now ({(now_ts-S2_START_TS)/86400:.1f} days)\n', flush=True)

    all_results = defaultdict(lambda: defaultdict(float))   # wallet → {quest: flares}
    all_events_by_wallet = defaultdict(list)                  # wallet → all events (both markets)
    all_snapshots = defaultdict(lambda: defaultdict(float))   # wallet → quest → final LP balance × rate
    all_cost_basis = defaultdict(dict)                        # wallet → quest → {usd_paid, usd_recovered, usd_basis, n_supplies, n_withdraws}
    skipped_quests = set()  # quests whose vault returned 0 sigs (RPC flake) — don't sync (would zero existing data)

    for mname, cfg in MARKETS.items():
        if cfg.get('version') == 'v2':
            # V2 attribution is the sibling tools/walk_v2_lp.py (Phase 5b).
            # 2026-06-07 multi-agent workflow tested 5 formulas; none fit all
            # 7 control rows naturally AND scaled safely system-wide. GPQs
            # USX-Sep26 stuck at 51.5% match — canary for a missing data
            # dimension (likely per-position fee accruals or an unindexed
            # V2 ix). See project_v2_clmm_lp_walker_2026_06_06.md.
            continue
        mult_tag = f'{cfg["boost_mult"]}× → {cfg["mult"]}×' if cfg.get('boost_mult') else f'{cfg["mult"]}×'
        peg_str = f'{cfg["peg"]:.4f}' if cfg.get('peg') is not None else '?'
        print(f'=== {mname} (mult {mult_tag}, peg ${peg_str}) ===', flush=True)
        # Walk the FULL LP-vault history. fetch_all_sigs has 4-retry-on-empty
        # protection but persistent RPC failure can still return 0. Don't let
        # that wipe existing wallet_quests data — track skipped quests and
        # exclude from sync.
        sigs = fetch_all_sigs(cfg['lp_vault'], 0)
        in_s2 = sum(1 for s in sigs if (s.get('blockTime') or 0) >= S2_START_TS)
        print(f'  {len(sigs):,} LP-vault sigs total ({in_s2:,} in S2 window)', flush=True)
        if len(sigs) == 0:
            # The LP vaults have thousands of sigs in their lifetime — 0 is
            # always an RPC failure, never legitimate.
            print(f'  WARN: skipping {cfg["quest"]} sync — RPC empty for known-active LP vault', flush=True)
            skipped_quests.add(cfg['quest'])
            continue

        # Fetch txs in parallel
        def fetch(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
                return s, r.get('result')
            except: return s, None

        # per-wallet event timeline (all events, both pre-S2 and S2)
        events_by_wallet = defaultdict(list)
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(fetch, s) for s in sigs]
            done = 0
            for fut in as_completed(futs):
                s, tx = fut.result()
                done += 1
                if done % 200 == 0: print(f'    {done}/{len(sigs)}', flush=True)
                if not tx: continue
                t = tx.get('blockTime') or 0
                parsed = parse_tx_lp_event(tx, cfg)
                if not parsed: continue
                events_by_wallet[parsed['user']].append({
                    't': t, 'lp_delta': parsed['lp_delta'],
                    'rate': parsed['per_lp_underlying'],
                    'sig': s['signature'],
                    'underlying_delta': parsed['underlying_delta'],
                    'market': cfg['market'],
                    'market_label': mname,
                    'underlying_mint': cfg['underlying'],
                })
        print(f'  unique LP-active wallets (this run): {len(events_by_wallet):,}', flush=True)

        # ============================================================
        # MONOTONIC EVENT STORE — persist every decoded LP event so a
        # single run's RPC flakes can never overwrite past captures. Cache
        # rebuilds use the FULL DB history, not this run's in-memory state.
        # See [[walk_s2_yt.py]] for the same pattern; LP differs because the
        # event's canonical lp_price (per_lp_underlying) is critical for
        # cost-basis math and CANNOT be reconstructed from amount_delta /
        # base_amount alone (slippage + fees diverge them). Store lp_price
        # in meta_json so re-reads recover the protocol-emitted value.
        # ============================================================
        import db as _db_es
        _db_es.init()
        events_to_persist = []
        for user, evs in events_by_wallet.items():
            for e in evs:
                events_to_persist.append({
                    'wallet': user, 'market': cfg['market'], 'sig': e['sig'], 'ts': e['t'],
                    'kind': 'add' if e['lp_delta'] > 0 else 'remove',
                    'amount_delta': e['lp_delta'],
                    'base_amount': e.get('underlying_delta') or 0.0,
                    'meta': {
                        'lp_price':         e.get('rate'),
                        'pt_delta':         e.get('pt_delta'),
                        'event_type':       e.get('event_type'),
                        'market_label':     mname,
                        'underlying_mint':  cfg['underlying'],
                    },
                })
        n_new = _db_es.put_walker_events('walk_s2_lp', events_to_persist)
        print(f'  walker_events: +{n_new:,} new rows (idempotent), '
              f'{len(events_to_persist)-n_new:,} already present', flush=True)

        # Replace this run's per-wallet events with the FULL persistent history.
        # Reconstruct using meta_json.lp_price (canonical, protocol-emitted) —
        # do NOT recompute from underlying_delta/lp_delta (slippage drift).
        full_rows = _db_es.get_walker_events_by_market('walk_s2_lp', cfg['market'])
        events_by_wallet = defaultdict(list)
        n_missing_meta = 0
        n_bad_rate = 0
        for r in full_rows:
            meta = r.get('meta') or {}
            lp_price = meta.get('lp_price')
            if lp_price is None:
                # Legacy row without meta — best-effort fallback. Should be 0
                # for fresh installs since we just populated above.
                n_missing_meta += 1
                lp_price = (r['base_amount'] / abs(r['amount_delta'])) if r['amount_delta'] else 0
            # Sanity-bound the rate. The derived fallback blows up when
            # amount_delta≈0 (the V2-withdraw decode bug → rate≈100K), and a bad
            # meta value would too. Integration uses sqrt(rate), so drop any event
            # whose rate is outside the plausible O(1) band rather than emit a blow-up.
            if lp_price is None or not (0 < lp_price < 100):
                n_bad_rate += 1
                continue
            events_by_wallet[r['wallet']].append({
                't': r['ts'], 'lp_delta': r['amount_delta'], 'rate': lp_price,
                'sig': r['sig'], 'underlying_delta': r['base_amount'],
                'market': cfg['market'], 'market_label': mname,
                'underlying_mint': cfg['underlying'],
                'pt_delta': meta.get('pt_delta', 0.0),
                'event_type': meta.get('event_type'),
            })
        if n_missing_meta:
            print(f'  ⚠️  {n_missing_meta:,} legacy walker_events rows missing meta_json '
                  f'(pre-meta-schema) — falling back to derived lp_price', flush=True)
        if n_bad_rate:
            print(f'  ⚠️  {n_bad_rate:,} walker_events rows dropped for out-of-range lp_price '
                  f'(bad-decode guard, rate∉(0,100))', flush=True)
        print(f'  walker_events: {sum(len(v) for v in events_by_wallet.values()):,} total events across '
              f'{len(events_by_wallet):,} wallets (full history)', flush=True)

        # Integrate LP × lp_price(t) × mult — no peg multiplication. Exponent's
        # lp_price_in_asset() is already denominated in the market's asset units
        # which Exponent treats 1:1 with USX (i.e. USD-equivalent). Applying the
        # eUSX peg here over-counts eUSX LP by ~14%; the user gets the eUSX
        # yield via the SY exchange rate which is baked into lp_price already.
        peg = cfg.get('peg') or 1.0
        for wallet, all_evs in events_by_wallet.items():
            all_evs.sort(key=lambda x: x['t'])

            # Cost basis: sum of USD invested minus USD recovered across all LP events
            # (pre-S2 + S2). LP price reflects current value of one LP token, so
            # USD per event = |lp_delta| × lp_price × peg. No time-decay needed
            # because lp_price itself evolves over time (unlike YT which decays).
            usd_paid = 0.0
            usd_recovered = 0.0
            n_supplies = 0
            n_withdraws = 0
            for e in all_evs:
                rate = e.get('rate') or 0
                if not rate: continue
                usd = abs(e['lp_delta']) * rate * peg
                if e['lp_delta'] > 0:
                    usd_paid += usd
                    n_supplies += 1
                else:
                    usd_recovered += usd
                    n_withdraws += 1
            if n_supplies + n_withdraws > 0:
                all_cost_basis[wallet][cfg['quest']] = {
                    'usd_basis':     max(0.0, usd_paid - usd_recovered),
                    'usd_paid':      usd_paid,
                    'usd_recovered': usd_recovered,
                    'n_supplies':    n_supplies,
                    'n_withdraws':   n_withdraws,
                }

            # Pre-S2 carry-in: net LP delta + lp_price snapshot at the boundary.
            pre_evs = [e for e in all_evs if e['t'] < S2_START_TS]
            evs = [e for e in all_evs if e['t'] >= S2_START_TS]
            carry_in = sum(e['lp_delta'] for e in pre_evs)
            if carry_in < 0: carry_in = 0.0  # data gap; clamp non-negative
            carry_price = None
            for e in reversed(pre_evs):
                if e.get('rate'):
                    carry_price = e['rate']; break

            lp_balance = carry_in
            last_lp_price = carry_price
            # Track usd_days separately for boost / base windows so per-segment
            # multipliers can be applied correctly when a market had a launch
            # boost that expired mid-S2 (Sep26 markets, boost ended 2026-06-02).
            boost_end = cfg.get('boost_end_ts')
            usd_days_boost = 0.0
            usd_days_base  = 0.0
            prev_t = S2_START_TS
            def _accum(usd_per_sec, t0, t1):
                """Split a [t0, t1] usd-flux into the boost / base buckets."""
                nonlocal usd_days_boost, usd_days_base
                if t1 <= t0: return
                if not boost_end or t0 >= boost_end:
                    usd_days_base += usd_per_sec * (t1 - t0) / 86400
                elif t1 <= boost_end:
                    usd_days_boost += usd_per_sec * (t1 - t0) / 86400
                else:
                    usd_days_boost += usd_per_sec * (boost_end - t0) / 86400
                    usd_days_base  += usd_per_sec * (t1 - boost_end) / 86400
            # Solstice values LP positions as `lp_balance × sqrt(lp_price)`,
            # not `lp_balance × lp_price`. Empirically calibrated via 5V9V's
            # 4 LP quests 2026-06-06: linear-rate walker was 143% over on
            # eUSX-Jun26 and 208% over on USX-Sep26; sqrt(rate) matches at
            # 99.5% / 98.2% (1.8% residual = peg precision). Likely a
            # liquidity-weighting convention (Curve-style invariant).
            for i in range(len(evs)):
                e = evs[i]
                t1 = e['t']
                if t1 > prev_t and lp_balance > 0 and last_lp_price:
                    _accum(lp_balance * math.sqrt(last_lp_price), prev_t, t1)
                lp_balance += e['lp_delta']
                if e.get('rate'): last_lp_price = e['rate']
                prev_t = t1
            # Tail: last event (or S2_START) to now
            if lp_balance > 0 and last_lp_price and prev_t < now_ts:
                _accum(lp_balance * math.sqrt(last_lp_price), prev_t, now_ts)
            base_mult  = cfg['mult']
            boost_mult = cfg.get('boost_mult', base_mult)
            flares = usd_days_base * base_mult + usd_days_boost * boost_mult
            if flares > 0:
                all_results[wallet][cfg['quest']] = flares
            # Store ALL events (pre-S2 + S2) in cache so build_daily_totals.py
            # can reconstruct the correct lp_balance(t) carry-in at S2_START.
            # The chart code clips pre-S2 segments via S2_START_TS in
            # integrate_balance_segments — but it needs the events to know the
            # pre-S2 balance. Without these, wallets holding LP from before S2
            # contribute 0 to the chart.
            all_events_by_wallet[wallet].extend(all_evs)
            if lp_balance > 0 and last_lp_price:
                # Snapshot stays in linear units (the cache field doubles as a
                # display value showing the user's notional USD-equivalent).
                all_snapshots[wallet][cfg['quest']] = lp_balance * last_lp_price

        # Quick stats for this market
        market_total = sum(r.get(cfg['quest'], 0) for r in all_results.values())
        top = sorted([(w, r.get(cfg['quest'], 0)) for w, r in all_results.items()], key=lambda x: -x[1])[:5]
        print(f'  total {cfg["quest"]} flares: {market_total:,.2f}')
        print(f'  top 5:')
        for w, f in top:
            if f > 0: print(f'    {w}  {f:,.2f}')
        print(flush=True)

        # Coverage: on-chain LP-token supply vs sum of wallet-tracked LP balances.
        # If on-chain > tracked, we missed wallet activity in this market.
        try:
            sup_r = rpc('getTokenSupply', [cfg['lp_mint']], timeout=15)
            sup_val = (sup_r.get('result') or {}).get('value', {})
            onchain_lp_ui = float(sup_val.get('uiAmount') or 0)
        except Exception:
            onchain_lp_ui = 0.0
        # Sum each wallet's net LP balance for this market from cached events.
        tracked_lp_ui = 0.0
        last_rate = None
        n_holders = 0
        for w_, evs_ in events_by_wallet.items():
            bal = sum(float(e.get('lp_delta') or 0) for e in evs_)
            if bal > 0:
                tracked_lp_ui += bal
                n_holders += 1
            for e in reversed(evs_):
                if e.get('rate'): last_rate = last_rate or e['rate']
        rate = last_rate or 1.0
        peg_val = (cfg.get('peg') or 1.0)
        walker_db.write_coverage('walk_s2_lp', cfg['quest'],
                                 pool_tvl_usd=onchain_lp_ui * rate * peg_val,
                                 tracked_tvl_usd=tracked_lp_ui * rate * peg_val,
                                 n_positions=n_holders)

    # Save
    out = {w: dict(r) for w, r in all_results.items()}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_lp_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    print(f'\nSaved {len(out)} wallets to {out_path}')

    # DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS_DB = [q for q in ['S2_EXPONENT_LP_USX_JUN26', 'S2_EXPONENT_LP_EUSX_JUN26',
                                     'S2_EXPONENT_LP_USX_SEP26', 'S2_EXPONENT_LP_EUSX_SEP26']
                        if q not in skipped_quests]
    if skipped_quests:
        print(f'NOTE: skipped sync for {skipped_quests} (RPC empty for active vault)', flush=True)
    walker_db.prune('walk_s2_lp')
    rows_db = []
    for w_, pq_ in out.items():
        for q_, v_ in pq_.items():
            if v_ > 0: rows_db.append((w_, q_, v_))
    walker_db.upsert_many('walk_s2_lp', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_lp', WALKER_QUESTS_DB)
    print(f'DB: walker_outputs={len(rows_db)} rows; synced to wallet_quests')

    # Per-wallet snapshot + event timeline → quest_cache (S2_EXPONENT_LP)
    # Format mirrors Orca/Raydium/Kamino/Loopscale so the drawer renders the
    # per-position cards consistently.
    import db
    db.init()
    snap_count = 0
    total_events = 0
    for wallet, evs in all_events_by_wallet.items():
        if not evs and not all_snapshots.get(wallet): continue
        # Transform LP-walker events into the drawer's event format
        drawer_events = []
        for e in evs:
            lp_d = e.get('lp_delta', 0)
            ix = 'increaseliquidity' if lp_d > 0 else 'decreaseliquidity'
            underlying_amt = abs(e.get('underlying_delta') or 0)
            drawer_events.append({
                'ts': e.get('t'),
                'ix': ix,
                'pos_pubkey': e.get('market'),                    # group by market
                'sig': e.get('sig'),
                'market_label': e.get('market_label'),
                'side': 'lp',
                'deltas': [{'mint': e.get('underlying_mint'), 'amt': underlying_amt}] if underlying_amt > 0 else [],
                'lp_delta': lp_d,                                 # preserved so we can recompute from cache
                'rate': e.get('rate'),                            # per_lp_underlying at this event
            })
        drawer_events.sort(key=lambda e: e.get('ts') or 0)
        snap_positions = {
            'usx_jun26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_USX_JUN26', 0), 2),
            'eusx_jun26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_EUSX_JUN26', 0), 2),
            'usx_sep26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_USX_SEP26', 0), 2),
            'eusx_sep26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_EUSX_SEP26', 0), 2),
        }
        snap = {
            'positions': snap_positions,
            'events': drawer_events,
            'cost_basis_by_quest': all_cost_basis.get(wallet, {}),
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(wallet, 'S2_EXPONENT_LP', snap, watermark_ts=now_ts)
        snap_count += 1
        total_events += len(drawer_events)
    print(f'Per-wallet snapshots written: {snap_count}  ({total_events} total events)')


if __name__ == '__main__':
    main()
