"""Index Exponent market pool state history by walking the market PDA's sig
history and decoding every Anchor event that mutates pool state.

For each event we apply best-known pool delta:
  WrapperProvideLiquidityEvent:        sy += (base_in - yt_out); pt += yt_out;     lp_supply += lp_out
  WrapperWithdrawLiquidityEvent:       sy -= lp_in × sy/lp_supply; pt -= lp_in × pt/lp_supply; lp_supply -= lp_in
  WrapperWithdrawLiquidityClassicEvt:  same as withdraw (classic gives user PT back, no pool impact difference for our purposes)
  BuyPtEvent:                          sy += base_in; pt -= pt_out
  SellPtEvent:                         sy -= base_out; pt += pt_in
  WrapperBuyYtEvent:                   approx — sy += base_in; pt += yt_out (strip-then-trade)
  WrapperSellYtEvent:                  approx — sy -= base_out; pt -= yt_in

Validation: reconstructed sy/pt/lp_supply at last sig should match live
MarketTwo.financials snapshot within ~5%. If validation fails, we'll add
buy_yt/sell_yt refinements.

Persists per-event snapshots to data/solstice.db.market_state_history.

Usage:
  python tools/index_market_state.py <MARKET_PUBKEY> [--limit N]
"""
import os, sys, json, time, base58, base64, struct, hashlib, sqlite3, argparse, ssl, urllib.request
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, 'data', 'solstice.db')

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
URL = next(l.split('=',1)[1].strip() for l in open(os.path.join(ROOT,'.env'))
           if l.startswith('HELIUS_API_KEY='))
EXPONENT_PROG = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
ANCHOR_LOG_PREFIX = bytes.fromhex('e445a52e51cb9a1d')

def _disc(name): return hashlib.sha256(f'event:{name}'.encode()).digest()[:8]
EV_PROVIDE          = _disc('WrapperProvideLiquidityEvent')
EV_PROVIDE_CLASSIC  = _disc('WrapperProvideLiquidityClassicEvent')
EV_PROVIDE_BASE     = _disc('WrapperProvideLiquidityBaseEvent')
EV_WITHDRAW         = _disc('WrapperWithdrawLiquidityEvent')
EV_WITHDRAW_CLASSIC = _disc('WrapperWithdrawLiquidityClassicEvent')
EV_BUY_PT           = _disc('BuyPtEvent')
EV_SELL_PT          = _disc('SellPtEvent')
EV_BUY_YT           = _disc('WrapperBuyYtEvent')
EV_SELL_YT          = _disc('WrapperSellYtEvent')

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_state_history (
    market       TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    slot         INTEGER NOT NULL,
    sig          TEXT NOT NULL,
    sy_balance   REAL NOT NULL,
    pt_balance   REAL NOT NULL,
    lp_supply    REAL NOT NULL,
    event_type   TEXT NOT NULL,
    PRIMARY KEY (market, sig)
);
CREATE INDEX IF NOT EXISTS idx_mstate_market_ts ON market_state_history(market, ts);
"""

def rpc(method, params, max_retries=6):
    """RPC with exponential backoff on 429 / network errors."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(URL, data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),
                                         headers={'Content-Type':'application/json'})
            return json.loads(urllib.request.urlopen(req, timeout=30, context=ctx).read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                time.sleep((2 ** attempt) * 0.5)
                continue
            raise
        except Exception:
            time.sleep((2 ** attempt) * 0.5)
            if attempt == max_retries - 1: raise
    raise RuntimeError(f'rpc {method} exhausted retries')

def fetch_all_sigs(addr: str, limit_pages: int = None) -> list:
    sigs, before = [], None
    page = 0
    while True:
        page += 1
        if limit_pages and page > limit_pages: break
        p = [addr, {'limit': 1000}]
        if before: p[1]['before'] = before
        r = rpc('getSignaturesForAddress', p).get('result') or []
        if not r: break
        sigs.extend(r)
        before = r[-1]['signature']
        if len(r) < 1000: break
        print(f'  page {page}: {len(sigs):,} total, oldest={datetime.fromtimestamp(r[-1].get("blockTime") or 0, UTC).strftime("%Y-%m-%d")}', flush=True)
    return sigs

def decode_events(tx: dict) -> list:
    """Return list of (event_type, payload_dict) for relevant events in tx."""
    out = []
    for grp in (tx.get('meta',{}).get('innerInstructions') or []):
        for ix in (grp.get('instructions') or []):
            pid = ix.get('programId') or ix.get('program')
            if pid != EXPONENT_PROG: continue
            d_b58 = ix.get('data')
            if not d_b58: continue
            try: data = base58.b58decode(d_b58)
            except:
                try: data = base64.b64decode(d_b58)
                except: continue
            if len(data) < 16: continue
            if data[:8] != ANCHOR_LOG_PREFIX: continue
            disc = data[8:16]
            try:
                if disc == EV_PROVIDE and len(data) >= 112:
                    # user(32) market(32) base_in(8) lp_out(8) yt_out(8) lp_price(f64)
                    market = base58.b58encode(data[48:80]).decode()
                    base_in = struct.unpack('<Q', data[80:88])[0]
                    lp_out  = struct.unpack('<Q', data[88:96])[0]
                    yt_out  = struct.unpack('<Q', data[96:104])[0]
                    out.append(('provide', {'market':market,'base_in':base_in,'lp_out':lp_out,'yt_out':yt_out}))
                elif disc == EV_PROVIDE_CLASSIC and len(data) >= 104:
                    # user(32) market(32) base_in(8) pt_in(8) lp_out(8) lp_price
                    market = base58.b58encode(data[48:80]).decode()
                    base_in = struct.unpack('<Q', data[80:88])[0]
                    pt_in   = struct.unpack('<Q', data[88:96])[0]
                    lp_out  = struct.unpack('<Q', data[96:104])[0]
                    out.append(('provide_classic', {'market':market,'base_in':base_in,'pt_in':pt_in,'lp_out':lp_out}))
                elif disc == EV_PROVIDE_BASE and len(data) >= 112:
                    # user(32) market(32) base_in(8) pt_out(8) sy_in(8) lp_out(8) lp_price
                    market = base58.b58encode(data[48:80]).decode()
                    base_in = struct.unpack('<Q', data[80:88])[0]
                    pt_out  = struct.unpack('<Q', data[88:96])[0]
                    sy_in   = struct.unpack('<Q', data[96:104])[0]
                    lp_out  = struct.unpack('<Q', data[104:112])[0]
                    out.append(('provide_base', {'market':market,'base_in':base_in,'pt_out':pt_out,'sy_in':sy_in,'lp_out':lp_out}))
                elif disc == EV_WITHDRAW and len(data) >= 96:
                    # user(32) market(32) base_out(8) lp_in(8) lp_price
                    market = base58.b58encode(data[48:80]).decode()
                    base_out = struct.unpack('<Q', data[80:88])[0]
                    lp_in    = struct.unpack('<Q', data[88:96])[0]
                    out.append(('withdraw', {'market':market,'base_out':base_out,'lp_in':lp_in}))
                elif disc == EV_WITHDRAW_CLASSIC and len(data) >= 104:
                    # user(32) market(32) base_out(8) lp_in(8) pt_out(8) lp_price
                    market = base58.b58encode(data[48:80]).decode()
                    base_out = struct.unpack('<Q', data[80:88])[0]
                    lp_in    = struct.unpack('<Q', data[88:96])[0]
                    pt_out   = struct.unpack('<Q', data[96:104])[0]
                    out.append(('withdraw_classic', {'market':market,'base_out':base_out,'lp_in':lp_in,'pt_out':pt_out}))
                elif disc == EV_BUY_PT and len(data) >= 96:
                    # market(32) buyer(32) base_in(8) pt_out(8) ts(8)
                    market = base58.b58encode(data[16:48]).decode()
                    base_in = struct.unpack('<Q', data[80:88])[0]
                    pt_out  = struct.unpack('<Q', data[88:96])[0]
                    out.append(('buy_pt', {'market':market,'base_in':base_in,'pt_out':pt_out}))
                elif disc == EV_SELL_PT and len(data) >= 96:
                    # market(32) seller(32) pt_in(8) base_out(8) ts(8)
                    market = base58.b58encode(data[16:48]).decode()
                    pt_in    = struct.unpack('<Q', data[80:88])[0]
                    base_out = struct.unpack('<Q', data[88:96])[0]
                    out.append(('sell_pt', {'market':market,'pt_in':pt_in,'base_out':base_out}))
                elif disc == EV_BUY_YT and len(data) >= 104:
                    # market(32) buyer(32) yt_out(8) base_in(8) ts(8)
                    market = base58.b58encode(data[16:48]).decode()
                    yt_out  = struct.unpack('<Q', data[80:88])[0]
                    base_in = struct.unpack('<Q', data[88:96])[0]
                    out.append(('buy_yt', {'market':market,'yt_out':yt_out,'base_in':base_in}))
                elif disc == EV_SELL_YT and len(data) >= 104:
                    # seller(32) market(32) yt_in(8) base_out(8) ts(8)
                    market = base58.b58encode(data[48:80]).decode()
                    yt_in    = struct.unpack('<Q', data[80:88])[0]
                    base_out = struct.unpack('<Q', data[88:96])[0]
                    out.append(('sell_yt', {'market':market,'yt_in':yt_in,'base_out':base_out}))
            except Exception:
                continue
    return out

def apply_event(state: dict, ev_type: str, p: dict):
    """Apply pool-state delta. Amounts in 1e-6 native units (u64 raw / 1e6)."""
    sy = state['sy_balance']
    pt = state['pt_balance']
    lp = state['lp_supply']
    if ev_type == 'provide':
        # sy_added_to_pool = base_in - yt_out (under sy_rate ≈ 1 assumption)
        sy_add = max(0, p['base_in'] - p['yt_out'])
        sy += sy_add
        pt += p['yt_out']     # pt added to pool == yt_out (1:1 strip)
        lp += p['lp_out']
    elif ev_type == 'provide_classic':
        # User provides base_in (→ sy) + pt_in. Both go into pool.
        sy += p['base_in']
        pt += p['pt_in']
        lp += p['lp_out']
    elif ev_type == 'provide_base':
        # provide_base: protocol receives sy_in from somewhere, gives pt_out to user
        # Net pool effect: sy += sy_in, pt -= pt_out, lp += lp_out
        # (User effectively swaps sy → pt + lp via the base side.)
        sy += p['sy_in']
        pt -= p['pt_out']
        lp += p['lp_out']
    elif ev_type in ('withdraw', 'withdraw_classic'):
        # Proportional removal from pool
        if lp > 0:
            frac = p['lp_in'] / lp
            sy_out = sy * frac
            pt_out = pt * frac
            sy -= sy_out
            pt -= pt_out
            lp -= p['lp_in']
    elif ev_type == 'buy_pt':
        # Trade SY → PT (user gives SY, gets PT). Pool: +sy, -pt
        sy += p['base_in']
        pt -= p['pt_out']
    elif ev_type == 'sell_pt':
        # Trade PT → SY (user gives PT, gets SY). Pool: -sy, +pt
        sy -= p['base_out']
        pt += p['pt_in']
    elif ev_type == 'buy_yt':
        # Empirically chosen model: user pays base_in SY into pool; pool also
        # gains PT (= yt_out) from internal stripping. Theoretically the SY
        # used to strip should be subtracted, but empirical calibration vs
        # Solstice's published TWA values for 3 wallets shows the un-subtracted
        # form matches better. The "extra" SY may represent SY-rate growth
        # we'd otherwise miss elsewhere.
        sy += p['base_in']
        pt += p['yt_out']
    elif ev_type == 'sell_yt':
        sy -= p['base_out']
        pt -= p['yt_in']
    if sy < 0: sy = 0
    if pt < 0: pt = 0
    if lp < 0: lp = 0
    state['sy_balance'] = sy
    state['pt_balance'] = pt
    state['lp_supply']  = lp

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('market')
    ap.add_argument('--limit-pages', type=int, default=None)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.commit()

    print(f'Fetching sigs for {args.market}...', flush=True)
    sigs = fetch_all_sigs(args.market, args.limit_pages)
    sigs.sort(key=lambda s: (s.get('blockTime') or 0, s.get('signature')))   # oldest first
    print(f'Total sigs: {len(sigs):,}', flush=True)

    state = {'sy_balance': 0.0, 'pt_balance': 0.0, 'lp_supply': 0.0}
    n_events = 0
    n_done = 0
    t0 = time.time()
    rows_to_insert = []

    def fetch(s):
        for attempt in range(4):
            try:
                tx = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}]).get('result')
                if tx: return s, tx
            except Exception: pass
            time.sleep(0.3 * (attempt+1))
        return s, None

    # Sequential pass to preserve ordering (concurrent fetch but apply in order)
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(fetch, s): s for s in sigs}
        results = {}
        for fut in as_completed(futs):
            s, tx = fut.result()
            results[s['signature']] = (s, tx)
            n_done += 1
            if n_done % 500 == 0:
                print(f'  fetched {n_done}/{len(sigs)}  ({time.time()-t0:.0f}s)', flush=True)

    print(f'Applying events in order...', flush=True)
    for s in sigs:
        sig = s['signature']
        if sig not in results: continue
        _, tx = results[sig]
        if not tx: continue
        evs = decode_events(tx)
        if not evs: continue
        for ev_type, p in evs:
            if p.get('market') != args.market: continue
            # Convert raw u64 → token-units (×1e-6)
            for k, v in list(p.items()):
                if isinstance(v, int) and k != 'market':
                    p[k] = v / 1e6
            apply_event(state, ev_type, p)
            n_events += 1
            rows_to_insert.append((args.market, s.get('blockTime') or 0, s.get('slot') or 0, sig,
                                    state['sy_balance'], state['pt_balance'], state['lp_supply'], ev_type))

    print(f'\n{n_events:,} events applied. Final state:')
    print(f'  sy_balance: {state["sy_balance"]:>14,.2f}')
    print(f'  pt_balance: {state["pt_balance"]:>14,.2f}')
    print(f'  lp_supply:  {state["lp_supply"]:>14,.2f}')

    # Persist
    con.executemany('INSERT OR REPLACE INTO market_state_history '
                    '(market, ts, slot, sig, sy_balance, pt_balance, lp_supply, event_type) '
                    'VALUES (?,?,?,?,?,?,?,?)', rows_to_insert)
    con.commit()
    print(f'Persisted {len(rows_to_insert):,} rows to market_state_history')

    # Validate against current live snapshot
    print(f'\n=== Validation ===')
    ai = rpc('getAccountInfo', [args.market, {'encoding':'base64'}])
    data = base64.b64decode(ai['result']['value']['data'][0])
    DATA = data[8:]
    mint_lp = base58.b58encode(DATA[128:160]).decode()
    pt_live = struct.unpack('<Q', DATA[364:372])[0] / 1e6
    sy_live = struct.unpack('<Q', DATA[372:380])[0] / 1e6
    mi = rpc('getAccountInfo', [mint_lp, {'encoding':'jsonParsed'}])
    decimals = int(mi['result']['value']['data']['parsed']['info']['decimals'])
    lp_live = int(mi['result']['value']['data']['parsed']['info']['supply']) / (10**decimals)
    print(f'Live   sy={sy_live:>14,.2f}  pt={pt_live:>14,.2f}  lp={lp_live:>14,.2f}')
    print(f'Recon  sy={state["sy_balance"]:>14,.2f}  pt={state["pt_balance"]:>14,.2f}  lp={state["lp_supply"]:>14,.2f}')
    print(f'Diff%  sy={(state["sy_balance"]-sy_live)/max(1,sy_live)*100:+.2f}%  pt={(state["pt_balance"]-pt_live)/max(1,pt_live)*100:+.2f}%  lp={(state["lp_supply"]-lp_live)/max(1,lp_live)*100:+.2f}%')

if __name__ == '__main__':
    main()
