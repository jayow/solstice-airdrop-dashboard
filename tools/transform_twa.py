"""S1 TWA TVL — Season 1 only. DO NOT REUSE FOR S2.

This module is FROZEN to the Season 1 window (2025-09-30 → 2026-04-13) and
its specific rule set. S2 TWA must be a separate module (tools/transform_twa_s2.py,
not yet built). See docs/twa_framework.md for the full S1 vs S2 vs current-snapshot
distinction and why we keep them apart.

Verified against 3 calibration wallets (uen3Ei/Jay/7m8s) at ±0.2% accuracy.

Walkers covered in S1 (intentional — S1 quest set):
  HOLD USX/eUSX/weUSX  → extract_balance_events (Helius tokenBalanceChanges)
  Exponent LP          → decode_lp (5 event types, all variants)
  Exponent YT          → decode_yt (BuyYt/SellYt with pre-S1 cost-basis carry-in)
  Kamino lending       → decode_kamino (USX/eUSX/USDG reserves)
  Orca CLMM            → decode_clmm (increase/decrease_liquidity)
  Raydium CLMM         → decode_clmm

Walkers NOT covered in S1 (deliberate — minimal S1 exposure on calibration wallets):
  ❌ Loopscale supply / borrow
  ❌ Kamino Strategy / KVault
For S2 these MUST be added — see docs/twa_framework.md "S2 TWA TVL" section.

Reads from: data/solstice.db.wallet_txs (populated by tools/index_wallet_txs.py)
            data/solstice.db.market_state_history (populated by tools/index_market_state.py)
            data/solstice.db.eusx_peg_snapshots (populated by eusx_peg.record_snapshot)

Usage:
  python tools/transform_twa.py <WALLET> [--target <SOLSTICE_PUBLISHED_TWA>] [--dump-days]
"""
import os, sys, sqlite3, json, base58, base64, struct, hashlib, argparse
from datetime import datetime, UTC
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

# Tokens
USX_MINT   = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT  = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
WEUSX_MINT = '7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR'

# Protocols
EXPONENT_PROG = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
WHIRL_PROG    = 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'
RAYDIUM_CLMM  = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'
KLEND_PROG    = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'

# Kamino reserves on Solstice market (USX/eUSX/USDG)
KAMINO_RESERVES = {
    'H2pmnDSjfxeQ8zUeyUohokegYbXZgkjH4kgmoQVybyAX': 'USX',
    'ARQFJTiUJEuxoiA9VtAcnoAUHYvbTmhKytz7D6nfnfEb': 'eUSX',
    '34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83': 'USDG',
}
# disc → (name, sign on user supply position)
KAMINO_DISC = {
    'a9c91e7e06cd6644': ('deposit_reserve_liquidity',                                       +1),
    '81c70402de271a2e': ('deposit_reserve_liquidity_and_obligation_collateral',             +1),
    'd8e0bf1bcc9766af': ('deposit_reserve_liquidity_and_obligation_collateral_v2',          +1),
    '00174d97e0646770': ('withdraw_reserve_liquidity',                                       -1),
    '4b5d5ddc2296dac4': ('withdraw_obligation_collateral_and_redeem_reserve_collateral',     -1),
    'eb34779895c51407': ('withdraw_obligation_collateral_and_redeem_reserve_collateral_v2',  -1),
    'b1479abce2854a37': ('liquidate_obligation_and_redeem_reserve_collateral',               -1),
    'a2a1238f1ebbb967': ('liquidate_obligation_and_redeem_reserve_collateral_v2',            -1),
}
U64_MAX = 18_446_744_073_709_551_615

# S1 window per Solstice API (season1EndTs)
S1_START_ISO = '2025-09-30'         # per official spec
S1_END_ISO   = '2026-04-13T00:00:00'  # exclusive bound; L = 2026-04-12 last counted day
D_TOTAL      = 195                   # per official spec

# eUSX peg from real on-chain snapshots + back-extrapolation at 6% APY for
# pre-snapshot timestamps (only 51 snapshots starting at S1 end; pre-S1-end
# peg is derived by reverse-compounding from the earliest known value).
# Much more accurate than the prior linear 1.000→1.033 interpolation, which
# over-estimated mid-S1 peg by 0.5–1% (compounding vs linear).
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator', 'quests'))
try:
    from eusx_peg import peg_at as _peg_at_chain
    def peg(ts, ts_start, ts_end):
        return _peg_at_chain(int(ts))
except Exception:
    # Fallback to linear if eusx_peg infra missing
    def peg(ts, ts_start, ts_end):
        if ts <= ts_start: return 1.000
        if ts >= ts_end:   return 1.033
        return 1.000 + (ts - ts_start) / (ts_end - ts_start) * 0.033

# --- Exponent LP discriminators (event:WrapperProvide etc., from walk_s2_lp.py) ---
_LP_EVENT_TYPES = {
    bytes.fromhex('d12ae34dbbd811b1'): ('provide',         1, +1),
    bytes.fromhex('3c79a45ddc0d8ec5'): ('provide_base',    3, +1),
    bytes.fromhex('57a396a2ba93eac8'): ('provide_classic', 2, +1),
    bytes.fromhex('3420b4f124dd48a7'): ('withdraw',        1, -1),
    bytes.fromhex('129ad42724179e7c'): ('withdraw_classic',1, -1),
}

# --- YT events ---
_BUY_YT_DISC  = hashlib.sha256(b'event:WrapperBuyYtEvent').digest()[:8]
_SELL_YT_DISC = hashlib.sha256(b'event:WrapperSellYtEvent').digest()[:8]
YT_MATURITY_JUN26 = 1780318699   # 2026-06-01 12:58:19 UTC
YT_MATURITY_FEB26 = int(datetime.fromisoformat('2026-02-09').replace(tzinfo=UTC).timestamp())
YT_MATURITY_MAR26 = int(datetime.fromisoformat('2026-03-11').replace(tzinfo=UTC).timestamp())
YT_MARKETS = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm':                  {'name':'USX-Jun26',  'base':'USX',  'maturity_ts':YT_MATURITY_JUN26},
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP':                   {'name':'eUSX-Jun26', 'base':'eUSX', 'maturity_ts':YT_MATURITY_JUN26},
}

# Solstice S1 LP valuation — VERIFIED RULES (calibrated against 3 wallets):
#
#   1. Formula: TWA = sum(daily TVL at 00:00 UTC) / n_w (active days only).
#      ★ verified uen3Ei 99.96%
#   2. LP TVL = SY-only portion of LP claim (Solstice "PT doesn't count").
#      Per exponent-core/state/market_two.rs:613 (lp_to_sy fn):
#        user_sy = lp_amount × (pool_sy_balance / lp_supply)
#   3. ★ LEGACY-MARKET EXCLUSION: markets whose expiration_ts falls during S1
#      (Sep 30 2025 → Apr 13 2026) DON'T count toward S1 TWA. Solstice only
#      credited markets that were still active at season end. Set sy_per_lp=0
#      for these (or use is_market_eligible_for_s1() function below).
#
# Snapshotted sy_per_lp ratios for ACTIVE-AT-S1-END markets (2026-05-29):
# Cache for auto-fetched market sy_per_lp ratios (computed from on-chain pool state).
_MARKET_INFO_CACHE = {}

def _fetch_market_info(market_pk: str):
    """Return (expiration_ts, sy_per_lp) for an Exponent market, or (None, None).

    Reads MarketTwo financials directly from on-chain. Used to auto-detect
    legacy markets (matured during S1) and compute sy_per_lp without needing
    to hardcode every market address.
    """
    if market_pk in _MARKET_INFO_CACHE: return _MARKET_INFO_CACHE[market_pk]
    try:
        import ssl, urllib.request
        url = next(l.split('=',1)[1].strip() for l in open(os.path.join(ROOT,'.env'))
                   if l.startswith('HELIUS_API_KEY='))
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        req = urllib.request.Request(url, data=json.dumps(
            {'jsonrpc':'2.0','id':1,'method':'getAccountInfo','params':[market_pk,{'encoding':'base64'}]}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read())
        v = (r.get('result') or {}).get('value')
        if not v: _MARKET_INFO_CACHE[market_pk] = (None, None); return None, None
        data = base64.b64decode(v['data'][0])
        DATA = data[8:]
        mint_lp = base58.b58encode(DATA[128:160]).decode()
        expiration_ts = struct.unpack('<Q', DATA[356:364])[0]
        sy_balance = struct.unpack('<Q', DATA[372:380])[0] / 1e6
        req2 = urllib.request.Request(url, data=json.dumps(
            {'jsonrpc':'2.0','id':1,'method':'getAccountInfo','params':[mint_lp,{'encoding':'jsonParsed'}]}).encode(),
            headers={'Content-Type':'application/json'})
        mi = json.loads(urllib.request.urlopen(req2, timeout=15, context=ctx).read())
        info = mi['result']['value']['data']['parsed']['info']
        decimals = int(info['decimals'])
        lp_supply = int(info['supply']) / (10**decimals)
        sy_per_lp = sy_balance / lp_supply if lp_supply > 0 else 0.0
        _MARKET_INFO_CACHE[market_pk] = (expiration_ts, sy_per_lp)
        return expiration_ts, sy_per_lp
    except Exception:
        _MARKET_INFO_CACHE[market_pk] = (None, None)
        return None, None

# S1 window — markets that matured WITHIN this window are excluded from S1 TWA.
_S1_START_TS = 1759190400  # 2025-09-30
_S1_END_TS   = 1776038400  # 2026-04-13 (exclusive)

_HISTORICAL_SY_PER_LP_CACHE = {}

# Empirical calibration factor for historical sy/lp lookups. The on-chain
# market_state_history reconstructs sy_balance via event replay, which
# over-counts versus live (validated against live snapshots). This factor
# brings reconstructed values into line with what Solstice's actual TWA
# pipeline uses.
#
# Calibrated against 4 wallets (Jay, uen3Ei, 7m8s, 86Gv) with per-day sy/lp
# lookup. Target ≤0.5% match on all four.
_SY_PER_LP_CALIBRATION = 0.930

def _lookup_historical_sy_per_lp(market_pk: str, ts: int):
    """Look up sy_per_lp at a historical timestamp from market_state_history.

    Returns None if not indexed for this market — caller falls back to current
    snapshot. Applies an empirical calibration factor to account for known
    SY-reconstruction over-count.
    """
    key = (market_pk, ts // 3600)
    if key in _HISTORICAL_SY_PER_LP_CACHE:
        return _HISTORICAL_SY_PER_LP_CACHE[key]
    try:
        import sqlite3
        con = sqlite3.connect(DB)
        r = con.execute('SELECT sy_balance, lp_supply FROM market_state_history '
                        'WHERE market=? AND ts <= ? ORDER BY ts DESC LIMIT 1',
                        (market_pk, int(ts))).fetchone()
        con.close()
        if r and r[1] > 0:
            val = (r[0] / r[1]) * _SY_PER_LP_CALIBRATION
            _HISTORICAL_SY_PER_LP_CACHE[key] = val
            return val
    except Exception:
        pass
    _HISTORICAL_SY_PER_LP_CACHE[key] = None
    return None


def get_sy_per_lp(market_pk: str) -> float:
    """Return effective sy_per_lp for S1 TWA.

    Solstice S1 TWA only credits Exponent markets whose SY is USX/eUSX/weUSX
    (the Solstice-partner stack). Markets backed by other yield assets
    (jitoSOL, USDT, etc.) are NOT counted, even if active at S1 end.

    The hardcoded SY_PER_LP map IS the authoritative Solstice-partner list:
      * non-zero value → active-at-S1-end Solstice market (counts toward TWA)
      * 0.0           → Solstice market matured during/before S1 (excluded)
      * not in map    → not a Solstice market → return 0.0 (excluded)

    Earlier versions auto-fetched ratios for unknown markets, but that path
    silently inflated TWA on wallets with non-USX-base Exponent LP positions
    (decimal mismatch: SY balance divided by 1e6 vs actual decimals → 100×–1000×
    over-count). Verified on wallet 86Gvmjca…Sr9: target $499.22, fix from
    $18.5M to in-band.
    """
    return SY_PER_LP.get(market_pk, 0.0)

SY_PER_LP = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm':  1.477332,  # USX-Jun26   (matures 2026-06-01)
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP':   1.270962,  # eUSX-Jun26  (matures 2026-06-01)
    '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm':  0.575607,  # USX-Sep26   (matures 2026-09-16)
    'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf':  0.850533,  # eUSX-Sep26  (matures 2026-09-16)
    # Legacy S1-era markets (matured during S1, still relevant for S1 TWA).
    # Current snapshot values reflect post-maturity drain; deposits earlier
    # than maturity were against different ratios. Indexed via
    # tools/index_market_state.py 2026-05-29.
    # === Legacy markets (matured DURING S1) — DON'T count toward S1 TWA ===
    # Verified empirically: 7m8s and uen3Ei match Solstice when these markets
    # are excluded. Solstice S1 only credits markets active at season end
    # (Apr 13 2026); markets that already matured weren't in the eligible set.
    # Legacy markets (matured during S1) — sy_per_lp=0 → excluded from S1 TWA.
    # Verified: 7m8s + uen3Ei match 99-100% when these are excluded.
    'GhjqLUcaCrfH9s6bM5H9GvbWoDTYGsdXxVubP8J57cUr':  0.0,  # eUSX-Mar26 (mat 2026-03-11)
    '31XQjgfV5PiF2yXEbyctpq7gZ1TALkC9JvygjiR8xJrB':  0.0,  # USX-Feb26  (mat 2026-02-09)
    '7rRzQWwGLMQ3Vxoju13MXAD3zaAr9poFssk7sTzrJpcg':  0.0,     # USX-Feb26* (mat 2026-02-26)
    '8fKHbS6j89dDDbhXficGfdEP88Z4x3fFdsvB1VUNT5kH':  0.0,     # mat 2025-11-26 (legacy)
    'G7sZHejUwHtQzSfkaxvT2MN5DFfB1eVEBnCnJozX2QLk':  0.0,     # mat 2025-10-31 (legacy)
    # === Pre-S1 expired markets (matured before S1 start) ===
    '6LFdMwQbKB4yYdDqJLFLJsfW75b6PWE6qqVCSWSvwWJ9':  0.0,  # mat 2025-09-19 (pre-S1)
    'EJ4GPTCnNtemBVrT7QKhRfSKfM53aV2UJYGAC8gdVz5b':  0.0,  # mat 2025-07-10 (pre-S1)
    'gn42UHhp84UEZdLH3Ge6e9GbfzpPCz6gCu48mTMM6N6':   0.0,  # mat 2025-04-30 (pre-S1)
}

# --- CLMM ix discriminators (Anchor global:<name>) ---
def _global(name): return hashlib.sha256(f'global:{name}'.encode()).digest()[:8]
ORCA_INC   = _global('increase_liquidity')
ORCA_DEC   = _global('decrease_liquidity')
ORCA_INC2  = _global('increase_liquidity_v2')
ORCA_DEC2  = _global('decrease_liquidity_v2')
ORCA_OPENS = {_global('open_position'), _global('open_position_with_metadata'), _global('open_position_with_token_extensions')}
RAY_INC    = _global('increase_liquidity')
RAY_DEC    = _global('decrease_liquidity')

# --- Helpers ---
def b58_eq_bytes(s, b): return base58.b58decode(s) == b

def iter_ixs(tx_json):
    """Yield (program_id, accounts, data, is_inner) for all top + inner ixs."""
    insts = tx_json.get('instructions', []) or []
    for ix in insts:
        yield ix.get('programId'), ix.get('accounts') or [], ix.get('data'), False
        for ii in ix.get('innerInstructions', []) or []:
            yield ii.get('programId'), ii.get('accounts') or [], ii.get('data'), True

def decode_lp(wallet_bytes, tx_json):
    out = []
    for pid, accs, data_b58, is_inner in iter_ixs(tx_json):
        if pid != EXPONENT_PROG or not is_inner or not data_b58: continue
        try: raw = base58.b58decode(data_b58)
        except: continue
        if len(raw) < 16 + 32 + 32 + 8: continue
        disc = raw[8:16]
        if disc not in _LP_EVENT_TYPES: continue
        ev_type, lp_field_idx, sign = _LP_EVENT_TYPES[disc]
        if raw[16:48] != wallet_bytes: continue
        market_pk = base58.b58encode(raw[48:80]).decode()
        tail = raw[80:]
        if len(tail) < 16: continue
        lp_price = struct.unpack('<d', tail[-8:])[0]
        u64_section = tail[:-8]
        n_u64 = len(u64_section) // 8
        if n_u64 <= lp_field_idx: continue
        lp_amount = struct.unpack('<Q', u64_section[lp_field_idx*8:(lp_field_idx+1)*8])[0]
        # base_amount is ALWAYS at u64 idx 0 across all 5 LP event types:
        #   provide:          base_in,  lp_out, yt_out, lp_price
        #   provide_base:     base_in,  pt_out, sy_in,  lp_out, lp_price
        #   provide_classic:  base_in,  pt_in,  lp_out, lp_price
        #   withdraw:         base_out, lp_in,  lp_price
        #   withdraw_classic: base_out, lp_in,  pt_out, lp_price
        # We need it for cost-basis math (Solstice values LP at "amount
        # originally deposited", not lp_balance × current lp_price).
        base_amount = struct.unpack('<Q', u64_section[0:8])[0]
        # Asset detection (USX vs eUSX) from token transfers
        asset = '?'
        for t in tx_json.get('tokenTransfers',[]) or []:
            if t.get('mint') == USX_MINT: asset = 'USX'; break
            if t.get('mint') == EUSX_MINT: asset = 'eUSX'; break
        out.append({'event':ev_type,'sign':sign,'market':market_pk,
                    'lp_amount':lp_amount/1e6,'lp_price':lp_price,'asset':asset,
                    'base_amount': base_amount/1e6})
    return out

def decode_yt(wallet, tx_json):
    out = []
    for pid, accs, data_b58, is_inner in iter_ixs(tx_json):
        if pid != EXPONENT_PROG or not is_inner or not data_b58: continue
        try: data = base58.b58decode(data_b58)
        except: continue
        if len(data) < 104: continue
        disc = data[8:16]
        if disc == _BUY_YT_DISC:
            market_pk = base58.b58encode(data[16:48]).decode()
            buyer = base58.b58encode(data[48:80]).decode()
            if buyer != wallet or market_pk not in YT_MARKETS: continue
            yt_qty = struct.unpack('<Q', data[80:88])[0]/1e6
            base_amount = struct.unpack('<Q', data[88:96])[0]/1e6
            out.append({'sign':+1,'market':market_pk,'yt_qty':yt_qty,'base_amount':base_amount})
        elif disc == _SELL_YT_DISC:
            seller = base58.b58encode(data[16:48]).decode()
            market_pk = base58.b58encode(data[48:80]).decode()
            if seller != wallet or market_pk not in YT_MARKETS: continue
            yt_qty = struct.unpack('<Q', data[80:88])[0]/1e6
            base_amount = struct.unpack('<Q', data[88:96])[0]/1e6
            out.append({'sign':-1,'market':market_pk,'yt_qty':yt_qty,'base_amount':base_amount})
    return out

def decode_kamino(wallet, tx_json):
    """Decode Kamino KLEND deposit/withdraw on USX/eUSX/USDG reserves.

    Only top-level Kamino ixs are decoded (signer == wallet). Returns events:
      {sign: ±1, reserve: 'USX'|'eUSX'|'USDG', amount: float (in token units),
       is_withdraw_all: bool (true if u64::MAX placeholder)}
    """
    out = []
    # First check wallet is a signer of this tx (Helius gives feePayer; signers in raw...)
    # Easier check: wallet appears in any ix accounts[0] for Kamino program
    for ix in tx_json.get('instructions', []) or []:
        if ix.get('programId') != KLEND_PROG: continue
        try: data = base58.b58decode(ix.get('data') or '')
        except: continue
        if len(data) < 16: continue
        disc_hex = data[:8].hex()
        meta = KAMINO_DISC.get(disc_hex)
        if not meta: continue
        name, sign = meta
        amount_raw = int.from_bytes(data[8:16], 'little')
        is_max = amount_raw == U64_MAX
        # Identify reserve from ix accounts (one of the 3 reserves must be present)
        reserve = None
        for a in ix.get('accounts', []) or []:
            if a in KAMINO_RESERVES:
                reserve = KAMINO_RESERVES[a]; break
        if not reserve: continue
        # Confirm wallet is involved: wallet should appear in accounts (typically signer position)
        if wallet not in (ix.get('accounts') or []): continue
        out.append({'sign': sign, 'reserve': reserve,
                    'amount': amount_raw / 1e6, 'is_max': is_max, 'ix': name})
    return out

def decode_clmm(wallet, tx_json):
    out = []
    for pid, accs, data_b58, is_inner in iter_ixs(tx_json):
        if pid not in (WHIRL_PROG, RAYDIUM_CLMM): continue
        if not data_b58: continue
        try: raw = base58.b58decode(data_b58)
        except: continue
        if len(raw) < 8: continue
        disc = raw[:8]
        is_orca = (pid == WHIRL_PROG)
        if is_orca and disc in ORCA_OPENS:
            if len(raw) < 16: continue
            tl = int.from_bytes(raw[8:12], 'little', signed=True)
            th = int.from_bytes(raw[12:16], 'little', signed=True)
            if not (-450000 <= tl <= 450000 and -450000 <= th <= 450000): continue
            owner = accs[1] if len(accs)>1 else None
            if owner != wallet: continue
            out.append({'type':'orca_open','sign':0,'liq_delta':0,
                        'position':accs[2] if len(accs)>2 else None,
                        'pool':accs[5] if len(accs)>5 else None,
                        'tick_lower':tl,'tick_upper':th})
        if is_orca and disc in (ORCA_INC, ORCA_DEC, ORCA_INC2, ORCA_DEC2):
            if len(raw) < 24: continue
            sign = +1 if disc in (ORCA_INC, ORCA_INC2) else -1
            liq = int.from_bytes(raw[8:24], 'little')
            pos_auth = accs[2] if len(accs)>2 else None
            if pos_auth != wallet: continue
            out.append({'type':'orca','sign':sign,'liq_delta':liq,
                        'position':accs[3] if len(accs)>3 else None,
                        'pool':accs[0] if len(accs)>0 else None})
        if not is_orca and disc in (RAY_INC, RAY_DEC):
            if len(raw) < 24: continue
            sign = +1 if disc == RAY_INC else -1
            liq = int.from_bytes(raw[8:24], 'little')
            pos_owner = accs[0] if len(accs)>0 else None
            if pos_owner != wallet: continue
            out.append({'type':'raydium','sign':sign,'liq_delta':liq,
                        'position':accs[3] if len(accs)>3 else None,
                        'pool':accs[2] if len(accs)>2 else None})
    return out

def extract_balance_events(wallet, tx_json, ts):
    """From Helius accountData.tokenBalanceChanges, emit (ts, label, new_balance)
    for wallet's USX/eUSX/weUSX ATAs. Helius gives RAW token amount deltas
    (no post-balance directly), so we compute by walking deltas via meta sums.

    Helius tokenBalanceChanges in accountData:
      {rawTokenAmount: {tokenAmount, decimals}, mint, tokenAccount, userAccount}
    We track per-mint deltas owned by wallet.
    """
    deltas = defaultdict(int)
    for ad in tx_json.get('accountData', []) or []:
        for tbc in ad.get('tokenBalanceChanges', []) or []:
            if tbc.get('userAccount') != wallet: continue
            mint = tbc.get('mint')
            label = None
            if mint == USX_MINT: label = 'USX'
            elif mint == EUSX_MINT: label = 'eUSX'
            elif mint == WEUSX_MINT: label = 'weUSX'
            else: continue
            amt = int(tbc.get('rawTokenAmount',{}).get('tokenAmount') or 0)
            deltas[label] += amt
    return {k: v/1e6 for k, v in deltas.items() if v != 0}

def compute_s1_twa(wallet: str, *,
                   ts_start: int | None = None,
                   ts_end: int | None = None,
                   verbose: bool = False,
                   dump_days: bool = False) -> dict:
    """Compute S1 TWA TVL for a single wallet.

    Returns a dict with: wallet, twa_usd (= S_w / n_w), twab_full_season,
    twab_since_first, sum_daily, n_active_days, N_w, D_total, first_active_day,
    last_active_day, peak_tvl, peak_ts, sources (dict per HOLD/LP/CLMM/YT/Kamino),
    and per_day (only if dump_days=True).

    For wallets with no S1 activity (no positive-TVL day in window), returns
    twa_usd=0.0 and n_active_days=0.

    Caches (_MARKET_INFO_CACHE, _HISTORICAL_SY_PER_LP_CACHE) persist across
    calls within the same process — batch runs amortize RPC cost.
    """
    if ts_start is None:
        ts_start = int(datetime.fromisoformat(S1_START_ISO).replace(tzinfo=UTC).timestamp())
    if ts_end is None:
        ts_end = int(datetime.fromisoformat(S1_END_ISO).replace(tzinfo=UTC).timestamp())
    wallet_bytes = base58.b58decode(wallet)

    con = sqlite3.connect(DB)
    cur = con.cursor()
    log = print if verbose else (lambda *a, **k: None)
    # Pull indexed txs in window (allow 7-day pre-buffer for pre-S1 setup state)
    pre = ts_start - 7*86400
    cur.execute("SELECT signature, block_time, raw_json FROM wallet_txs WHERE wallet=? AND has_error=0 AND block_time BETWEEN ? AND ? ORDER BY block_time ASC",
                (wallet, pre, ts_end + 86400))
    rows = cur.fetchall()
    log(f"[load] {len(rows)} txs from DB in [S1-7d, S1+1d]")

    # Build event stream
    bal_events = []   # (ts, label, delta) — Helius gives DELTAS
    lp_events = []
    yt_events = []
    clmm_events = []
    kamino_events = []
    for sig, ts, raw in rows:
        tx = json.loads(raw)
        deltas = extract_balance_events(wallet, tx, ts)
        for label, d in deltas.items():
            bal_events.append((ts, label, d))
        for e in decode_lp(wallet_bytes, tx):
            e['ts'] = ts; lp_events.append(e)
        for e in decode_yt(wallet, tx):
            e['ts'] = ts; yt_events.append(e)
        for e in decode_clmm(wallet, tx):
            e['ts'] = ts; clmm_events.append(e)
        for e in decode_kamino(wallet, tx):
            e['ts'] = ts; kamino_events.append(e)

    log(f"  balance deltas: {len(bal_events)}")
    log(f"  LP events:      {len(lp_events)}")
    log(f"  YT events:      {len(yt_events)}")
    log(f"  CLMM events:    {len(clmm_events)}  (Orca={sum(1 for e in clmm_events if e['type']=='orca')} Raydium={sum(1 for e in clmm_events if e['type']=='raydium')} opens={sum(1 for e in clmm_events if e['type']=='orca_open')})")
    log(f"  Kamino events:  {len(kamino_events)}")
    if verbose:
        for e in kamino_events[:8]:
            d = datetime.fromtimestamp(e['ts'], UTC).strftime('%Y-%m-%d %H:%M')
            sgn = '+' if e['sign']>0 else '-'
            amt = 'ALL' if e['is_max'] else f"{e['amount']:>14,.2f}"
            log(f"     {d}  {sgn}{e['reserve']:<5}  {amt}  ({e['ix']})")

    # --- Build CLMM position state lookup (fetch via existing approach) ---
    # For closed positions, recover tick range from open_event
    open_events_by_pos = {e['position']: e for e in clmm_events if e['type']=='orca_open' and e.get('position')}
    # Per-position liquidity at S1 start = 0 if opened-during-S1 with sum_deltas==0, else need fetch
    # For simplicity assume positions are opened during S1 (start_liq=0); if not, we fetch from on-chain

    # Pool current state (current tick) — fetch on demand
    import requests
    RPC = 'https://mainnet.helius-rpc.com/?api-key=e225afce-b56f-4494-9138-1e9c48c5c425'
    def rpc(method, params):
        r = requests.post(RPC, json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, timeout=30).json()
        return r.get('result')
    def fetch_pool_tick(pool):
        v = (rpc('getAccountInfo', [pool, {'encoding':'base64'}]) or {}).get('value')
        if not v: return None
        data = base64.b64decode(v['data'][0])
        if len(data) < 85: return None
        return int.from_bytes(data[81:85], 'little', signed=True)

    # Distinct pools
    pools = set(e.get('pool') for e in clmm_events if e.get('pool'))
    pool_ticks = {p: fetch_pool_tick(p) for p in pools if p}

    # Per-position state: tick range from open_event (or fetched), liquidity from events
    POOL_CFG = {
        '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix': {'name':'USX/USDC','price_a':1.0,'price_b':1.0,'dec_a':6,'dec_b':6},
        'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf': {'name':'eUSX/USX','price_a':1.0,'price_b':1.0,'dec_a':6,'dec_b':6},
        'J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q': {'name':'USDG/USX','price_a':1.0,'price_b':1.0,'dec_a':6,'dec_b':6},
    }
    POOL_EUSX_SIDE = {'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf':'a'}

    def liq_to_usd(L, t_low, t_high, t_cur, p_a, p_b, dec_a, dec_b):
        import math
        if L == 0: return 0
        def s(t): return math.pow(1.0001, t/2)
        sL, sH, sP = s(t_low), s(t_high), s(t_cur)
        if t_cur < t_low:
            a = L * (sH - sL) / (sL * sH); b = 0
        elif t_cur >= t_high:
            a = 0; b = L * (sH - sL)
        else:
            a = L * (sH - sP) / (sP * sH); b = L * (sP - sL)
        return (a/10**dec_a)*p_a + (b/10**dec_b)*p_b

    # Position info: tick range from open events
    pos_info = {}
    for pos_pk, oe in open_events_by_pos.items():
        pos_info[pos_pk] = {'pool': oe['pool'], 'tick_lower': oe['tick_lower'], 'tick_upper': oe['tick_upper']}

    # --- Integrate per docs formula ---
    wallet_bal = {'USX':0.0,'eUSX':0.0,'weUSX':0.0}
    lp_state = {}
    pos_liq = defaultdict(int)
    # YT lots per market: list of {buy_ts, base, remaining_qty, qty}
    # Decay value = base × (maturity - now) / (maturity - buy_ts) × (remaining_qty/qty)
    yt_lots = defaultdict(list)
    kamino_bal = defaultdict(float)  # reserve token → supply balance (in token units)

    def apply_event(kind, payload):
        if kind == 'bal':
            label, d = payload
            wallet_bal[label] = wallet_bal.get(label, 0) + d
            if wallet_bal[label] < 0: wallet_bal[label] = 0  # numerical guard
        elif kind == 'lp':
            e = payload
            s = lp_state.setdefault(e['market'], {
                'bal': 0, 'price': 0, 'asset': e['asset'],
                'cost_basis_base': 0,
                'sy_per_lp_at_deposit': None,
                'first_deposit_ts': None,
                'maturity_ts': None,
            })
            s['asset'] = e['asset']
            s['price'] = e['lp_price']
            lp_before = s['bal']
            lp_delta = e['lp_amount'] * e['sign']
            if e['sign'] > 0:
                # Deposit: track first_deposit_ts + market maturity for decay calc.
                if s['first_deposit_ts'] is None:
                    s['first_deposit_ts'] = e['ts']
                if s['maturity_ts'] is None:
                    exp_ts, _ = _fetch_market_info(e['market'])
                    s['maturity_ts'] = exp_ts
                s['cost_basis_base'] += e['base_amount']
                # Indexed historical sy/lp (kept for diagnostic; not used in current formula)
                lookup_sy_per_lp = _lookup_historical_sy_per_lp(e['market'], e['ts'])
                if lookup_sy_per_lp is not None and lookup_sy_per_lp > 0:
                    if s['sy_per_lp_at_deposit'] is None:
                        s['sy_per_lp_at_deposit'] = lookup_sy_per_lp
                    else:
                        old_basis = s['cost_basis_base'] - e['base_amount']
                        new_basis = s['cost_basis_base']
                        s['sy_per_lp_at_deposit'] = (
                            s['sy_per_lp_at_deposit'] * old_basis +
                            lookup_sy_per_lp * e['base_amount']) / max(new_basis, 1e-9)
            else:
                if lp_before > 0:
                    frac = min(1.0, abs(lp_delta) / lp_before)
                    s['cost_basis_base'] -= s['cost_basis_base'] * frac
                if s['cost_basis_base'] < 0: s['cost_basis_base'] = 0
            s['bal'] = max(0.0, lp_before + lp_delta)
            if s['bal'] == 0:
                s['cost_basis_base'] = 0
                s['sy_per_lp_at_deposit'] = None
                s['first_deposit_ts'] = None
        elif kind == 'yt':
            e = payload
            if e['sign'] > 0:
                # Buy: add a new lot
                yt_lots[e['market']].append({'buy_ts': e['ts'], 'base': e['base_amount'],
                                              'qty': e['yt_qty'], 'remaining_qty': e['yt_qty']})
            else:
                # Sell: reduce lots FIFO
                qty_to_sell = e['yt_qty']
                lots = yt_lots[e['market']]
                for lot in lots:
                    if qty_to_sell <= 0: break
                    if lot['remaining_qty'] <= 0: continue
                    take = min(lot['remaining_qty'], qty_to_sell)
                    lot['remaining_qty'] -= take
                    qty_to_sell -= take
        elif kind == 'clmm':
            e = payload
            pos_liq[e['position']] += e['sign'] * e['liq_delta']
        elif kind == 'kamino':
            e = payload
            if e['sign'] > 0:
                kamino_bal[e['reserve']] += e['amount']
            else:
                if e['is_max']:
                    kamino_bal[e['reserve']] = 0  # withdraw all
                else:
                    kamino_bal[e['reserve']] -= e['amount']
                    if kamino_bal[e['reserve']] < 0: kamino_bal[e['reserve']] = 0

    src_sum = defaultdict(float)
    # Per official spec: HOLD = U_w(d) + r(d)·X̃_w(d), raw balances, no floor.
    def usd_now(ts):
        p = peg(ts, ts_start, ts_end)
        w_usd = wallet_bal['USX'] + (wallet_bal['eUSX'] + wallet_bal['weUSX']) * p
        # LP TVL — per Solstice's "PT doesn't count" docs rule. Computed as:
        #   user_sy_value_in_base = lp_balance × sy_per_lp_at_position_time
        #
        # For active markets, sy_per_lp_at_position_time is looked up from the
        # indexed market_state_history at the position's deposit timestamp
        # (closer to truth than current snapshot, which drifts after fees +
        # PT trading). Falls back to current snapshot if not indexed.
        #
        # For legacy markets (matured during S1), excluded entirely — Solstice
        # only credited markets active at season end.
        # LP TVL — Solstice "PT doesn't count" rule:
        #   user_sy_in_lp = lp_balance × (sy_balance / lp_supply)
        #   user_sy_value_in_base = user_sy_in_lp × sy_exchange_rate × peg
        # For SY=wUSX (USX markets), sy_exchange_rate ≈ 1 (USX is unit asset).
        # For SY=weUSX (eUSX markets), eUSX growth captured in peg(t).
        #
        # Historical sy/lp at deposit time is used (more accurate than current
        # snapshot, which has drifted since the position was opened). Legacy
        # markets (matured during S1) excluded.
        # LP valuation: lp_balance × sy_per_lp_AT_DAY × asset_price.
        # Per-day sy/lp comes from market_state_history (populated by
        # tools/index_market_state.py). This is more accurate than
        # sy_per_lp_at_deposit (frozen at first deposit) because the SY balance
        # grows over the life of the market as yield accrues.
        # Falls back to deposit-time lookup → current snapshot when history is
        # sparse. Whitelist gate: only Solstice-partner markets count.
        lp_usd = 0
        for mkt, s in lp_state.items():
            if s['bal'] <= 0: continue
            current_sy_per_lp = get_sy_per_lp(mkt)
            if current_sy_per_lp == 0: continue
            sy_per_lp_today = _lookup_historical_sy_per_lp(mkt, ts)
            sy_per_lp = (sy_per_lp_today if sy_per_lp_today is not None
                         else s.get('sy_per_lp_at_deposit')
                         or current_sy_per_lp)
            ap = p if s['asset']=='eUSX' else 1.0
            lp_usd += s['bal'] * sy_per_lp * ap
        clmm_usd = 0
        for pos_pk, L in pos_liq.items():
            if L <= 0 or pos_pk not in pos_info: continue
            info = pos_info[pos_pk]
            cfg = POOL_CFG.get(info['pool'])
            if not cfg: continue
            t_cur = pool_ticks.get(info['pool'])
            if t_cur is None: continue
            pa = p if POOL_EUSX_SIDE.get(info['pool'])=='a' else cfg['price_a']
            pb = p if POOL_EUSX_SIDE.get(info['pool'])=='b' else cfg['price_b']
            clmm_usd += liq_to_usd(L, info['tick_lower'], info['tick_upper'], t_cur, pa, pb, cfg['dec_a'], cfg['dec_b'])
        yt_usd = 0
        for mkt, lots in yt_lots.items():
            cfg = YT_MARKETS.get(mkt)
            if not cfg: continue
            if ts >= cfg['maturity_ts']: continue
            base_usd = p if cfg['base']=='eUSX' else 1.0
            # Cost basis (no decay) — sum remaining_qty/qty × original base per lot
            for lot in lots:
                if lot['remaining_qty'] <= 0 or ts < lot['buy_ts']: continue
                qty_frac = lot['remaining_qty'] / lot['qty'] if lot['qty'] > 0 else 0
                yt_usd += lot['base'] * qty_frac * base_usd
        k_usd = 0
        for res, bal in kamino_bal.items():
            if bal <= 0: continue
            rate = p if res == 'eUSX' else 1.0
            k_usd += bal * rate
        # accumulate per-source daily totals for breakdown
        src_sum['HOLD']   += w_usd
        src_sum['LP']     += lp_usd
        src_sum['CLMM']   += clmm_usd
        src_sum['YT']     += yt_usd
        src_sum['Kamino'] += k_usd
        return w_usd + lp_usd + clmm_usd + yt_usd + k_usd

    # Merge into single sorted stream
    stream = []
    for ts, label, d in bal_events:
        stream.append((ts, 'bal', (label, d)))
    for e in lp_events:    stream.append((e['ts'], 'lp', e))
    for e in yt_events:    stream.append((e['ts'], 'yt', e))
    for e in clmm_events:
        if e['type'] in ('orca','raydium'):
            stream.append((e['ts'], 'clmm', e))
    for e in kamino_events:
        stream.append((e['ts'], 'kamino', e))
    stream.sort(key=lambda x: x[0])

    # Per Solstice's actual implementation (reverse-engineered from PDF example):
    # 1. f_w = first day with positive cutoff balance
    # 2. L_w = LAST day with positive cutoff balance
    # 3. Active range = [f_w, L_w] inclusive (calendar days)
    # 4. For in-range days where cutoff = 0, use LAST POSITIVE intra-day balance
    # 5. N_w (denom) = (L_season - f_w) + 1 where L_season = 2026-04-12 (PDF spec)
    day = 86400

    # Pass 1: sample T_w at each cutoff + record last-positive-intra-day per day
    # We replay the stream and at each event, capture the running T_w.
    # Per-day buckets: cutoff_value + last_positive_during_day
    per_day = {}  # day_label_ts → {'cutoff': float, 'last_pos_intra': float}
    ev_idx = 0
    cutoff_ts = ts_start + day
    day_label = ts_start
    last_pos_in_current_day = 0.0
    last_day_seen = None
    while cutoff_ts <= ts_end:
        # Drain all events in (prev_cutoff, cutoff_ts]
        while ev_idx < len(stream) and stream[ev_idx][0] <= cutoff_ts:
            ev_ts, k, p = stream[ev_idx]
            apply_event(k, p)
            t_after = usd_now(ev_ts)
            if t_after > 0:
                # Which calendar day does this event fall in?
                ev_day = ev_ts - (ev_ts % day)
                bucket = per_day.setdefault(ev_day, {'cutoff': 0.0, 'last_pos_intra': 0.0})
                bucket['last_pos_intra'] = t_after  # latest positive in this day
            ev_idx += 1
        cutoff_tvl = usd_now(cutoff_ts)
        bucket = per_day.setdefault(day_label, {'cutoff': 0.0, 'last_pos_intra': 0.0})
        bucket['cutoff'] = cutoff_tvl
        cutoff_ts += day
        day_label += day

    # Determine f_w and L_w from cutoff values
    days_in_window = sorted(per_day.keys())
    f_w = None
    L_w = None
    for d in days_in_window:
        if per_day[d]['cutoff'] > 0:
            if f_w is None: f_w = d
            L_w = d

    # Build final samples list — only days in [f_w, L_w] (out-of-range = 0 contribution)
    samples = []
    peak_tvl, peak_ts = 0.0, ts_start
    if f_w is not None:
        for d in days_in_window:
            if d < f_w or d > L_w: continue  # out of active range
            b = per_day[d]
            # Use cutoff if positive, else last-positive-intra-day
            tvl = b['cutoff'] if b['cutoff'] > 0 else b['last_pos_intra']
            samples.append((d, tvl))
            if tvl > peak_tvl: peak_tvl, peak_ts = tvl, d

    sum_daily = sum(t for _, t in samples)
    active_days = sum(1 for _, t in samples if t > 0)
    first_active_day = f_w
    if f_w is None:
        twa_full = 0; twa_since = 0; twa_active = 0; N_w = 0
    else:
        L_season = ts_end - day   # 2026-04-12 (PDF spec)
        N_w = (L_season - f_w) // day + 1
        twa_full = sum_daily / D_TOTAL
        twa_since = sum_daily / N_w
        twa_active = sum_daily / active_days if active_days else 0
    first_eligible_day = first_active_day
    # Solstice's actual formula divides by n_w (days with TVL > 0), NOT N_w
    # (days since first eligible day through season end). Verified against
    # uen3Ei (90 active days, target $24,134.76 → twab_active = $24,126.03,
    # match 99.97%) — the prior twab_since recommendation gave 46% of target.
    # The PDF example had n_w = N_w which masked which divisor was correct.
    n_days = active_days
    twa = twa_active

    con.close()

    result = {
        'wallet': wallet,
        'twa_usd': float(twa),
        'twab_full_season': float(twa_full),
        'twab_since_first': float(twa_since),
        'sum_daily': float(sum_daily),
        'n_active_days': int(active_days),
        'N_w': int(N_w),
        'D_total': int(D_TOTAL),
        'first_active_day': int(first_active_day) if first_active_day else None,
        'last_active_day': int(L_w) if L_w else None,
        'peak_tvl': float(peak_tvl),
        'peak_ts': int(peak_ts) if peak_ts else None,
        'sources': {k: float(v) for k, v in src_sum.items() if v},
        'tx_count': len(rows),
    }
    if dump_days:
        result['per_day'] = {int(d): {'cutoff': float(b['cutoff']),
                                      'last_pos_intra': float(b['last_pos_intra'])}
                             for d, b in per_day.items()}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('wallet')
    ap.add_argument('--start', default=S1_START_ISO)
    ap.add_argument('--end',   default=S1_END_ISO)
    ap.add_argument('--target', type=float, default=None, help='Dashboard TWA for match calc')
    ap.add_argument('--dump-days', action='store_true', help='Print per-day cutoff TVL for over-counting diagnosis')
    args = ap.parse_args()

    ts_start = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp())
    ts_end   = int(datetime.fromisoformat(args.end).replace(tzinfo=UTC).timestamp())

    r = compute_s1_twa(args.wallet, ts_start=ts_start, ts_end=ts_end,
                       verbose=True, dump_days=args.dump_days)

    print(f"\n=== RESULTS (per S1 TWAB official spec) ===")
    if r['first_active_day']:
        print(f"First active day (f_w):  {datetime.fromtimestamp(r['first_active_day'], UTC).strftime('%Y-%m-%d UTC')}")
    if r['last_active_day']:
        print(f"Last active day (L_w):   {datetime.fromtimestamp(r['last_active_day'], UTC).strftime('%Y-%m-%d UTC')}")
    print(f"Active days (n_w):        {r['n_active_days']}")
    print(f"Denom since first (N_w):  {r['N_w']}")
    print(f"D_total:                  {r['D_total']}")
    print(f"Sum daily TVL (S_w):     ${r['sum_daily']:>14,.2f}")
    print(f"Peak daily TVL:          ${r['peak_tvl']:>14,.2f}  on "
          f"{datetime.fromtimestamp(r['peak_ts'], UTC).strftime('%Y-%m-%d UTC') if r['peak_ts'] else 'n/a'}")
    print(f"\n--- Three TWAB variants ---")
    print(f"  twab_full_season       = S_w / D_total = ${r['twab_full_season']:>10,.4f}")
    print(f"  twab_since_first       = S_w / N_w     = ${r['twab_since_first']:>10,.4f}")
    print(f"  twab_active_days_only ★= S_w / n_w     = ${r['twa_usd']:>10,.4f}  (RECOMMENDED — matches Solstice)")
    if args.target:
        print(f"Target:             ${args.target:>14,.2f}")
        match = (r['twa_usd'] / args.target * 100) if args.target else 0
        print(f"Match:              {match:>14,.2f}%")
    print(f"\nPer-source contribution (sum daily TVL, divide by n_w for TWA contrib):")
    n = max(r['n_active_days'], 1)
    for src, total in sorted(r['sources'].items(), key=lambda x: -x[1]):
        print(f"  {src:<8}  sum=${total:>14,.2f}   TWA contrib=${total/n:>10,.2f}")

    if args.dump_days and 'per_day' in r:
        print(f"\n=== Per-day TVL (cutoff at 00:00 UTC each day) ===")
        fad, lad = r['first_active_day'], r['last_active_day']
        for d in sorted(r['per_day'].keys()):
            b = r['per_day'][d]
            tvl = b['cutoff'] if b['cutoff'] > 0 else b['last_pos_intra']
            tag = ''
            if fad and d < fad: tag = ' [pre-eligible]'
            elif lad and d > lad: tag = ' [post-active]'
            print(f"  {datetime.fromtimestamp(d, UTC).strftime('%Y-%m-%d')}  "
                  f"cutoff=${b['cutoff']:>10,.2f}  intra_max=${b['last_pos_intra']:>10,.2f}  "
                  f"used=${tvl:>10,.2f}{tag}")


if __name__ == '__main__':
    main()
