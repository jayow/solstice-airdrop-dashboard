"""Transform indexed wallet_txs into S1 TWA TVL via docs formula.

Reads from data/solstice.db (populated by tools/index_wallet_txs.py).
Applies Solstice docs formula:
    TWA = sum(daily TVL at 00:00 UTC) / days since first eligible day

Components decoded (from Helius enhanced JSON):
  HOLD USX/eUSX/weUSX  → accountData.tokenBalanceChanges where owner=wallet
  Exponent LP          → innerInstructions emit_cpi events
  Exponent YT          → cost-basis: buy adds, sell subtracts (running)
  Orca/Raydium CLMM    → increase/decrease_liquidity ix with wallet as signer

Usage:
  python tools/transform_twa.py <WALLET> [--target <DASHBOARD_TWA>]
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

# eUSX peg drift (linear ~$1.000 → $1.033)
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

# Current pool sy_per_lp ratios — used by LP TVL formula to credit only the
# SY portion of an LP claim (Solstice's "PT doesn't count" rule). Snapshotted
# from on-chain MarketTwo state on 2026-05-29. Ratios drift over time as PT
# trades happen; historical ratios at deposit time are typically SMALLER for
# deposits made well before maturity (more PT-heavy pool). Using current
# ratio retroactively therefore UNDER-counts old deposits — but it's much
# closer than the cost-basis-only approach (which over-counted by 6×).
# Refresh via tools/snapshot_exponent_sy_ratios.py.
SY_PER_LP = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm':  1.477332,  # USX-Jun26
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP':   1.270962,  # eUSX-Jun26
    '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm':  0.575607,  # USX-Sep26
    'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf':  0.850533,  # eUSX-Sep26
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
    wallet = args.wallet
    wallet_bytes = base58.b58decode(wallet)

    con = sqlite3.connect(DB)
    cur = con.cursor()
    # Pull indexed txs in window (allow 7-day pre-buffer for pre-S1 setup state)
    pre = ts_start - 7*86400
    cur.execute("SELECT signature, block_time, raw_json FROM wallet_txs WHERE wallet=? AND has_error=0 AND block_time BETWEEN ? AND ? ORDER BY block_time ASC",
                (wallet, pre, ts_end + 86400))
    rows = cur.fetchall()
    print(f"[load] {len(rows)} txs from DB in [{args.start} - 7d, {args.end} + 1d]")

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

    print(f"  balance deltas: {len(bal_events)}")
    print(f"  LP events:      {len(lp_events)}")
    print(f"  YT events:      {len(yt_events)}")
    print(f"  CLMM events:    {len(clmm_events)}  (Orca={sum(1 for e in clmm_events if e['type']=='orca')} Raydium={sum(1 for e in clmm_events if e['type']=='raydium')} opens={sum(1 for e in clmm_events if e['type']=='orca_open')})")
    print(f"  Kamino events:  {len(kamino_events)}")
    for e in kamino_events[:8]:
        d = datetime.fromtimestamp(e['ts'], UTC).strftime('%Y-%m-%d %H:%M')
        sgn = '+' if e['sign']>0 else '-'
        amt = 'ALL' if e['is_max'] else f"{e['amount']:>14,.2f}"
        print(f"     {d}  {sgn}{e['reserve']:<5}  {amt}  ({e['ix']})")

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
            s = lp_state.setdefault(e['market'], {'bal':0,'price':0,'asset':e['asset'],'cost_basis_base':0})
            s['asset'] = e['asset']
            s['price'] = e['lp_price']   # kept for diagnostic only; no longer used by usd_now
            lp_before = s['bal']
            lp_delta = e['lp_amount'] * e['sign']
            if e['sign'] > 0:
                # Deposit: add base_amount paid (in underlying token units) to cost basis
                s['cost_basis_base'] += e['base_amount']
            else:
                # Withdraw: reduce cost basis proportionally to LP removed
                # (per "amount originally deposited" rule — Solstice's accounting).
                if lp_before > 0:
                    frac = min(1.0, abs(lp_delta) / lp_before)
                    s['cost_basis_base'] -= s['cost_basis_base'] * frac
                if s['cost_basis_base'] < 0: s['cost_basis_base'] = 0
            s['bal'] = max(0.0, lp_before + lp_delta)
            if s['bal'] == 0: s['cost_basis_base'] = 0    # fully withdrawn → no residual basis
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
        # LP TVL = user's SY share of the LP pool (per Solstice's "PT doesn't
        # count" rule, docs.solstice.finance/.../flares/season-2). Computed as:
        #     user_sy = lp_balance × (pool_sy_balance / lp_supply)
        #     lp_usd  = user_sy × sy_exchange_rate × peg
        # SY_PER_LP is the current pool ratio per market (decoded from
        # MarketTwo.financials via fetch_market_sy_ratios). Using current
        # ratio retroactively under-counts deposits made earlier (when pool
        # was more PT-heavy) — best we can do without historical pool state.
        # Source: exponent-core/state/market_two.rs:613 (lp_to_sy fn).
        lp_usd = 0
        for mkt, s in lp_state.items():
            if s['bal'] <= 0: continue
            ap = p if s['asset']=='eUSX' else 1.0
            sy_per_lp = SY_PER_LP.get(mkt, 1.0)
            user_sy = s['bal'] * sy_per_lp
            lp_usd += user_sy * ap
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

    print(f"\n=== RESULTS (per S1 TWAB official spec) ===")
    if first_active_day:
        print(f"First active day (f_w):  {datetime.fromtimestamp(first_active_day, UTC).strftime('%Y-%m-%d UTC')}")
    if L_w:
        print(f"Last active day (L_w):   {datetime.fromtimestamp(L_w, UTC).strftime('%Y-%m-%d UTC')}")
    print(f"Active days (n_w):        {active_days}")
    print(f"Denom since first (N_w):  {N_w}")
    print(f"D_total:                  {D_TOTAL}")
    print(f"Sum daily TVL (S_w):     ${sum_daily:>14,.2f}")
    print(f"Peak daily TVL:          ${peak_tvl:>14,.2f}  on {datetime.fromtimestamp(peak_ts, UTC).strftime('%Y-%m-%d UTC') if peak_ts else 'n/a'}")
    print(f"\n--- Three TWAB variants ---")
    print(f"  twab_full_season       = S_w / D_total = ${twa_full:>10,.4f}")
    print(f"  twab_since_first       = S_w / N_w     = ${twa_since:>10,.4f}")
    print(f"  twab_active_days_only ★= S_w / n_w     = ${twa_active:>10,.4f}  (RECOMMENDED — matches Solstice)")
    if args.target:
        print(f"Target:             ${args.target:>14,.2f}")
        print(f"Match:              {twa/args.target*100:>14,.2f}%")
    print(f"\nPer-source contribution (sum daily TVL, divide by N for TWA contrib):")
    for src, total in sorted(src_sum.items(), key=lambda x:-x[1]):
        if total == 0: continue
        contrib = total / max(n_days,1)
        print(f"  {src:<8}  sum=${total:>14,.2f}   TWA contrib=${contrib:>10,.2f}")

    # --dump-days: print per-day cutoff TVL to identify over-counting days
    if getattr(args, 'dump_days', False):
        print(f"\n=== Per-day TVL (cutoff at 00:00 UTC each day) ===")
        for d in sorted(per_day.keys()):
            b = per_day[d]
            tvl = b['cutoff'] if b['cutoff'] > 0 else b['last_pos_intra']
            tag = ''
            if d < (first_active_day or 0): tag = ' [pre-eligible]'
            elif L_w and d > L_w: tag = ' [post-active]'
            print(f"  {datetime.fromtimestamp(d, UTC).strftime('%Y-%m-%d')}  "
                  f"cutoff=${b['cutoff']:>10,.2f}  intra_max=${b['last_pos_intra']:>10,.2f}  "
                  f"used=${tvl:>10,.2f}{tag}")

if __name__ == '__main__':
    main()
