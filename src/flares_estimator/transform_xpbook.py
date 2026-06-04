"""Derive per-(wallet, orderbook) net flow from xpbook_events — v3.

Re-runnable: idempotent. Drop + rebuild every time. No RPC.

All byte layouts come from @exponent-labs/exponent-sdk v0.9.18 Codama codecs.
See package/build/client/orderbook/types/*.js.

Empirically corrected semantics (v0.9.18 IDL is partially out-of-date)
----------------------------------------------------------------------
- orderTypeFlag (postOfferEvent byte 108):
    1 = SellYt   (deposit YT, list to sell; amountIn is YT raw)
    2 = BuyYt    (deposit USX, list to buy;  amountIn is USX raw)
  (IDL v0.9.18 has these inverted as SellYt=0, BuyYt=1; production uses 1/2.)

- removeOfferEvent.amountOut is NOT a refund to the user. It marks the slab
  position being freed. USX/YT stays in the orderbook escrow.

- Real USX refunds come ONLY from wrapperWithdrawFundsEvent.amountSyOut.
- Real YT deliveries come from wrapperWithdrawFundsEvent.amountYtOut.

Cost basis formula (no estimation — all values from raw events)
---------------------------------------------------------------
For each (wallet, orderbook):

  usx_deposited     = Σ wrapperPostOfferEvent.amountDeposit
                      WHERE companion postOfferEvent.orderTypeFlag = 2 (BuyYt)
  usx_sy_refunded   = Σ wrapperWithdrawFundsEvent.amountSyOut
  yt_received       = Σ wrapperWithdrawFundsEvent.amountYtOut
  pt_received       = Σ wrapperWithdrawFundsEvent.amountPtOut
  actual_fill_cost  = Σ filledOffersEvent.takenAmount across embedded fills in
                      postOfferEvent + marketOfferEvent where this wallet is the
                      maker (looked up via (orderbook, offer_idx) → most recent
                      prior post). Filtered to BuyYt-side maker offers only —
                      for SellYt makers, takenAmount is YT not USX.
  escrow_residual   = usx_deposited - usx_sy_refunded - actual_fill_cost
                      (the USX still locked in orderbook escrow on open offers)

Maker attribution edge case: if two posts on the same (orderbook, offer_idx)
share the same blocktime, the lookup is non-deterministic. Low risk in practice.
"""
import os, sys, struct, time
import base58

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as _db

SCHEMA = """
DROP TABLE IF EXISTS xpbook_fills;
CREATE TABLE xpbook_fills (
    fill_sig         TEXT NOT NULL,
    fill_blocktime   INTEGER NOT NULL,
    orderbook        TEXT NOT NULL,
    maker_slot       INTEGER NOT NULL,
    maker            TEXT,                  -- looked up from xpbook_offers; NULL if no prior post
    yt_filled_raw    INTEGER NOT NULL,      -- YT delivered to maker (BuyYt) / taken from maker (SellYt)
    usx_taken_raw    INTEGER NOT NULL,      -- USX consumed (BuyYt: from maker escrow → taker)
    amount_base_raw  INTEGER NOT NULL,
    taker_fee_raw    INTEGER NOT NULL,
    maker_fee_raw    INTEGER NOT NULL,
    source_event     TEXT NOT NULL,         -- 'postOfferEvent' | 'marketOfferEvent'
    PRIMARY KEY (fill_sig, orderbook, maker_slot, source_event)
);
CREATE INDEX ix_xpbook_fills_maker ON xpbook_fills(maker, orderbook);
CREATE INDEX ix_xpbook_fills_slot ON xpbook_fills(orderbook, maker_slot, fill_blocktime);

DROP TABLE IF EXISTS xpbook_wallet_orderbook_flow;
CREATE TABLE xpbook_wallet_orderbook_flow (
    wallet                  TEXT NOT NULL,
    orderbook               TEXT NOT NULL,
    usx_deposited_raw       INTEGER NOT NULL,   -- BuyYt posts only
    usx_sy_refunded_raw     INTEGER NOT NULL,   -- wrapperWithdrawFundsEvent.amountSyOut
    yt_received_raw         INTEGER NOT NULL,   -- wrapperWithdrawFundsEvent.amountYtOut
    pt_received_raw         INTEGER NOT NULL,   -- wrapperWithdrawFundsEvent.amountPtOut
    yt_deposited_raw        INTEGER NOT NULL,   -- SellYt posts only
    actual_fill_cost_raw    INTEGER NOT NULL,   -- Σ usx_taken from fills where this wallet was maker
    escrow_residual_raw     INTEGER NOT NULL,   -- usx_deposited - usx_sy_refunded - actual_fill_cost
    n_buy_posts             INTEGER NOT NULL,
    n_sell_posts            INTEGER NOT NULL,
    n_removes               INTEGER NOT NULL,
    n_withdraws             INTEGER NOT NULL,
    n_fills_as_maker        INTEGER NOT NULL,
    PRIMARY KEY (wallet, orderbook)
);
CREATE INDEX ix_xpbook_flow_wallet ON xpbook_wallet_orderbook_flow(wallet);
CREATE INDEX ix_xpbook_flow_orderbook ON xpbook_wallet_orderbook_flow(orderbook);

DROP TABLE IF EXISTS xpbook_offers;
CREATE TABLE xpbook_offers (
    orderbook        TEXT NOT NULL,
    offer_idx        INTEGER NOT NULL,
    maker            TEXT NOT NULL,
    side             TEXT NOT NULL,        -- 'SellYt' (flag=1) | 'BuyYt' (flag=2) | 'Unknown(N)'
    price_apy_bps    INTEGER NOT NULL,
    amount_in_raw    INTEGER NOT NULL,
    amount_deposit_raw INTEGER NOT NULL,
    post_sig         TEXT NOT NULL,
    post_blocktime   INTEGER NOT NULL,
    PRIMARY KEY (orderbook, offer_idx, post_sig)
);
CREATE INDEX ix_xpbook_offers_maker ON xpbook_offers(maker);

DROP TABLE IF EXISTS xpbook_claims;
CREATE TABLE xpbook_claims (
    sig                  TEXT NOT NULL,
    blocktime            INTEGER NOT NULL,
    trader               TEXT NOT NULL,
    orderbook            TEXT NOT NULL,
    amount_pt_out_raw    INTEGER NOT NULL,
    amount_yt_out_raw    INTEGER NOT NULL,
    amount_sy_out_raw    INTEGER NOT NULL,
    PRIMARY KEY (sig, trader, orderbook)
);
CREATE INDEX ix_xpbook_claims_trader ON xpbook_claims(trader, blocktime);

DROP TABLE IF EXISTS xpbook_escrow_timeline;
CREATE TABLE xpbook_escrow_timeline (
    wallet          TEXT NOT NULL,
    orderbook       TEXT NOT NULL,
    asset_kind      TEXT NOT NULL,    -- 'SY' (USX/eUSX wrapped) | 'YT'
    event_blocktime INTEGER NOT NULL,
    delta_raw       INTEGER NOT NULL, -- +amount on post, -amount on fill/withdraw
    event_kind      TEXT NOT NULL,    -- 'post' | 'fill' | 'withdraw'
    event_sig       TEXT NOT NULL,
    PRIMARY KEY (event_sig, wallet, orderbook, asset_kind, event_kind, event_blocktime, delta_raw)
);
CREATE INDEX ix_xpbook_escrow_timeline_wallet ON xpbook_escrow_timeline(wallet, asset_kind, event_blocktime);
CREATE INDEX ix_xpbook_escrow_timeline_ob ON xpbook_escrow_timeline(orderbook, asset_kind, event_blocktime);
"""

# Scope: only Solstice S2 quest markets (USX/eUSX Sep26 maturities).
# All other orderbooks (EkMwpJy1, etc.) are out of scope for flares.
S2_ORDERBOOKS = {
    'A2yaEiehRCvibSdMWWJtrBdmVCYwGRNSNwg1VwdicthU': 'USX-Sep26',
    '3mXbVuMynj21doFXXEauJ2tGDV9kS2Q1SnnQDcgD54Bw': 'eUSX-Sep26',
}


# ---- IDL-faithful codec offsets ----

# wrapperPostOfferEvent (208B): user(32) + orderbook(32) + vault(32) + sySrc(32) +
#                               ytSrc(32) + ptSrc(32) + amountDeposit(u64) + amountBaseIn(u64)
WPOE_USER          = 0
WPOE_ORDERBOOK     = 32
WPOE_AMOUNT_DEPOSIT = 192

# postOfferEvent (185B for empty fills): user(32) + orderbook(32) + vault(32) +
#                                        priceImpliedApy(u32) + amountIn(u64) +
#                                        orderTypeFlag(u8) + virtualOffer(bool) +
#                                        expirySeconds(u32) + options(2B) +
#                                        amountStripped/Merged/Out (3×u64) +
#                                        offerIdx(Option<u32>) + ...
POE_USER          = 0
POE_ORDERBOOK     = 32
POE_PRICE_APY     = 96
POE_AMOUNT_IN     = 100
POE_ORDER_TYPE    = 108
POE_OFFER_IDX_OPT = 140

# wrapperWithdrawFundsEvent (88B): trader(32) + orderbook(32) +
#                                  amountPtOut(u64) + amountYtOut(u64) + amountSyOut(u64)
WWFE_TRADER         = 0
WWFE_ORDERBOOK      = 32
WWFE_AMOUNT_PT_OUT  = 64
WWFE_AMOUNT_YT_OUT  = 72
WWFE_AMOUNT_SY_OUT  = 80


def _init_schema(con):
    con.executescript(SCHEMA)


def _pk(p: bytes, off: int) -> str:
    return base58.b58encode(p[off:off+32]).decode()


def _u64(p: bytes, off: int) -> int:
    return struct.unpack('<Q', p[off:off+8])[0]


def _u32(p: bytes, off: int) -> int:
    return struct.unpack('<I', p[off:off+4])[0]


def _decode_fills_vec(payload: bytes, start_off: int) -> list:
    """Decode Array<filledOffersEvent> at offset start_off.
    Returns list of (slot, yt_filled, usx_taken, amt_base, taker_fee, maker_fee)."""
    if len(payload) < start_off + 4: return []
    n = struct.unpack('<I', payload[start_off:start_off+4])[0]
    fills = []
    off = start_off + 4
    for _ in range(n):
        if off + 44 > len(payload): break
        fills.append((
            struct.unpack('<I', payload[off+8:off+12])[0],   # slot (offer_idx)
            struct.unpack('<Q', payload[off:off+8])[0],      # yt_filled
            struct.unpack('<Q', payload[off+12:off+20])[0],  # usx_taken
            struct.unpack('<Q', payload[off+20:off+28])[0],  # amount_base
            struct.unpack('<Q', payload[off+28:off+36])[0],  # taker_fee
            struct.unpack('<Q', payload[off+36:off+44])[0],  # maker_fee
        ))
        off += 44
    return fills


def _build_fills(con):
    """Extract every fill from marketOfferEvent + postOfferEvent embedded vecs.
    Attribute each fill to the maker via xpbook_offers (most recent post on
    that slot before the fill blocktime)."""

    fills_to_insert = []

    # postOfferEvent embedded fills (fired by takers)
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'postOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 149: continue
        ob = _pk(p, POE_ORDERBOOK)
        # offerIdx is Option<u32>: 1 byte tag at 140
        if p[POE_OFFER_IDX_OPT] == 1:   fills_off = POE_OFFER_IDX_OPT + 5
        elif p[POE_OFFER_IDX_OPT] == 0: fills_off = POE_OFFER_IDX_OPT + 1
        else: continue
        for (slot, yt, usx, amt_base, tf, mf) in _decode_fills_vec(p, fills_off):
            fills_to_insert.append((r['sig'], r['blocktime'], ob, slot,
                                     yt, usx, amt_base, tf, mf, 'postOfferEvent'))

    # marketOfferEvent fills — fills vec at offset 116
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'marketOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 120: continue
        ob = _pk(p, 32)
        for (slot, yt, usx, amt_base, tf, mf) in _decode_fills_vec(p, 116):
            fills_to_insert.append((r['sig'], r['blocktime'], ob, slot,
                                     yt, usx, amt_base, tf, mf, 'marketOfferEvent'))

    # Attribute maker per fill via xpbook_offers lookup
    # (most recent post on same (orderbook, offer_idx) before fill_blocktime)
    rows = []
    for fill in fills_to_insert:
        sig, bt, ob, slot, yt, usx, ab, tf, mf, src = fill
        maker_row = con.execute(
            "SELECT maker FROM xpbook_offers "
            "WHERE orderbook = ? AND offer_idx = ? AND post_blocktime <= ? "
            "ORDER BY post_blocktime DESC LIMIT 1",
            (ob, slot, bt)).fetchone()
        maker = maker_row[0] if maker_row else None
        rows.append((sig, bt, ob, slot, maker, yt, usx, ab, tf, mf, src))

    con.executemany(
        "INSERT OR IGNORE INTO xpbook_fills "
        "(fill_sig, fill_blocktime, orderbook, maker_slot, maker, "
        " yt_filled_raw, usx_taken_raw, amount_base_raw, "
        " taker_fee_raw, maker_fee_raw, source_event) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    n_attributed = sum(1 for r in rows if r[4] is not None)
    print(f'  xpbook_fills: {len(rows)} fills ({n_attributed} attributed to maker)')


def _build_wallet_flows(con):
    """Aggregate per (wallet, orderbook) using actual fill data for cost basis."""

    # Map sig → orderTypeFlag from postOfferEvent
    poe_flag = {}
    for r in con.execute("SELECT sig, payload FROM xpbook_events "
                          "WHERE event_name = 'postOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 109: continue
        poe_flag[r['sig']] = p[POE_ORDER_TYPE]

    flows = {}
    def _get(w, ob):
        k = (w, ob)
        if k not in flows:
            flows[k] = {'usx_dep': 0, 'sy_ref': 0, 'yt_in': 0, 'pt_in': 0,
                        'yt_dep_sell': 0, 'fill_cost': 0,
                        'n_buy_posts': 0, 'n_sell_posts': 0,
                        'n_removes': 0, 'n_withdraws': 0, 'n_fills': 0}
        return flows[k]

    # Deposits — wrapperPostOfferEvent
    for r in con.execute("SELECT sig, payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperPostOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 200: continue
        wallet = _pk(p, WPOE_USER)
        ob = _pk(p, WPOE_ORDERBOOK)
        amt = _u64(p, WPOE_AMOUNT_DEPOSIT)
        flag = poe_flag.get(r['sig'])
        if flag is None: continue
        d = _get(wallet, ob)
        if flag == 2:
            d['usx_dep'] += amt
            d['n_buy_posts'] += 1
        elif flag == 1:
            d['yt_dep_sell'] += amt
            d['n_sell_posts'] += 1

    # Withdrawals — wrapperWithdrawFundsEvent
    for r in con.execute("SELECT payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperWithdrawFundsEvent'"):
        p = bytes(r['payload'])
        if len(p) < 88: continue
        wallet = _pk(p, WWFE_TRADER)
        ob = _pk(p, WWFE_ORDERBOOK)
        d = _get(wallet, ob)
        d['sy_ref']     += _u64(p, WWFE_AMOUNT_SY_OUT)
        d['yt_in']      += _u64(p, WWFE_AMOUNT_YT_OUT)
        d['pt_in']      += _u64(p, WWFE_AMOUNT_PT_OUT)
        d['n_withdraws'] += 1

    # Removes — count only
    for r in con.execute("SELECT payload FROM xpbook_events "
                          "WHERE event_name = 'removeOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 173: continue
        _get(_pk(p, 0), _pk(p, 32))['n_removes'] += 1

    # Actual fill cost — sum usx_taken from xpbook_fills, but ONLY for fills where
    # the maker's offer was BuyYt. SellYt makers' takenAmount is YT (not USX) —
    # would falsely inflate cost basis if summed as USX.
    for r in con.execute("""
        SELECT f.maker, f.orderbook,
               SUM(f.usx_taken_raw) total_taken, COUNT(*) n
        FROM xpbook_fills f
        JOIN xpbook_offers o
          ON o.orderbook = f.orderbook
         AND o.offer_idx = f.maker_slot
         AND o.post_blocktime = (
             SELECT MAX(post_blocktime) FROM xpbook_offers
             WHERE orderbook = f.orderbook AND offer_idx = f.maker_slot
               AND post_blocktime <= f.fill_blocktime)
        WHERE f.maker IS NOT NULL AND o.side = 'BuyYt'
        GROUP BY f.maker, f.orderbook
    """):
        d = _get(r['maker'], r['orderbook'])
        d['fill_cost'] += r['total_taken'] or 0
        d['n_fills'] += r['n'] or 0

    rows = []
    for (w, ob), d in flows.items():
        residual = d['usx_dep'] - d['sy_ref'] - d['fill_cost']
        rows.append((w, ob, d['usx_dep'], d['sy_ref'], d['yt_in'], d['pt_in'],
                     d['yt_dep_sell'], d['fill_cost'], residual,
                     d['n_buy_posts'], d['n_sell_posts'],
                     d['n_removes'], d['n_withdraws'], d['n_fills']))
    con.executemany(
        "INSERT INTO xpbook_wallet_orderbook_flow "
        "(wallet, orderbook, usx_deposited_raw, usx_sy_refunded_raw, "
        " yt_received_raw, pt_received_raw, yt_deposited_raw, "
        " actual_fill_cost_raw, escrow_residual_raw, "
        " n_buy_posts, n_sell_posts, n_removes, n_withdraws, n_fills_as_maker) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f'  xpbook_wallet_orderbook_flow: {len(rows)} (wallet, orderbook) rows')


def _build_offers_inspection(con):
    """Per-post inspection — same as v2 with corrected side labels."""
    wpoe_by_sig = {}
    for r in con.execute("SELECT sig, payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperPostOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 200: continue
        wpoe_by_sig[r['sig']] = _u64(p, WPOE_AMOUNT_DEPOSIT)

    rows = []
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'postOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 145: continue
        try:
            maker = _pk(p, POE_USER)
            orderbook = _pk(p, POE_ORDERBOOK)
            price_apy = _u32(p, POE_PRICE_APY)
            amount_in = _u64(p, POE_AMOUNT_IN)
            flag = p[POE_ORDER_TYPE]
            if p[POE_OFFER_IDX_OPT] != 1: continue
            offer_idx = _u32(p, POE_OFFER_IDX_OPT + 1)
        except Exception:
            continue
        side = ('SellYt' if flag == 1 else
                'BuyYt'  if flag == 2 else
                f'Unknown({flag})')
        rows.append((orderbook, offer_idx, maker, side, price_apy,
                     amount_in, wpoe_by_sig.get(r['sig'], 0),
                     r['sig'], r['blocktime']))
    con.executemany(
        "INSERT OR IGNORE INTO xpbook_offers "
        "(orderbook, offer_idx, maker, side, price_apy_bps, amount_in_raw, "
        " amount_deposit_raw, post_sig, post_blocktime) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    print(f'  xpbook_offers: {len(rows)} posts indexed')


def _build_escrow_timeline(con):
    """Per-(wallet, orderbook, asset_kind) signed-delta event series for S2 markets.

    Consumers integrate this against time to add escrow balances to TWAB:
       balance(t) = Σ delta_raw for events with event_blocktime <= t
    """
    rows = []

    # Map sig → flag from postOfferEvent (companion to wrapperPostOfferEvent)
    poe_flag = {}
    for r in con.execute("SELECT sig, payload FROM xpbook_events "
                          "WHERE event_name = 'postOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 109: continue
        poe_flag[r['sig']] = p[POE_ORDER_TYPE]

    # Deposits — wrapperPostOfferEvent
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperPostOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 200: continue
        ob = _pk(p, WPOE_ORDERBOOK)
        if ob not in S2_ORDERBOOKS: continue
        flag = poe_flag.get(r['sig'])
        if flag not in (1, 2): continue
        asset_kind = 'SY' if flag == 2 else 'YT'   # BuyYt deposits SY, SellYt deposits YT
        wallet = _pk(p, WPOE_USER)
        amt = _u64(p, WPOE_AMOUNT_DEPOSIT)
        rows.append((wallet, ob, asset_kind, r['blocktime'], amt, 'post', r['sig']))

    # Fills — for each match where this wallet was the maker, emit BOTH sides:
    #   - drain on the asset they had escrowed (BuyYt maker loses SY; SellYt loses YT)
    #   - credit on the asset they received (BuyYt maker gets YT into escrow;
    #     SellYt gets SY) until they explicitly withdraw it
    for r in con.execute("""
        SELECT f.maker, f.orderbook, f.fill_blocktime, f.fill_sig,
               f.usx_taken_raw, f.yt_filled_raw, f.amount_base_raw, o.side
        FROM xpbook_fills f
        JOIN xpbook_offers o
          ON o.orderbook = f.orderbook
         AND o.offer_idx = f.maker_slot
         AND o.post_blocktime = (
             SELECT MAX(post_blocktime) FROM xpbook_offers
             WHERE orderbook = f.orderbook AND offer_idx = f.maker_slot
               AND post_blocktime <= f.fill_blocktime)
        WHERE f.maker IS NOT NULL
    """):
        if r['orderbook'] not in S2_ORDERBOOKS: continue
        if r['side'] == 'BuyYt':
            # SY taken from maker; YT delivered (escrowed until withdraw)
            rows.append((r['maker'], r['orderbook'], 'SY',
                         r['fill_blocktime'], -r['usx_taken_raw'],
                         'fill', r['fill_sig']))
            rows.append((r['maker'], r['orderbook'], 'YT',
                         r['fill_blocktime'], r['yt_filled_raw'],
                         'fill', r['fill_sig']))
        elif r['side'] == 'SellYt':
            # YT taken from maker; SY delivered (escrowed until withdraw)
            rows.append((r['maker'], r['orderbook'], 'YT',
                         r['fill_blocktime'], -r['usx_taken_raw'],
                         'fill', r['fill_sig']))
            rows.append((r['maker'], r['orderbook'], 'SY',
                         r['fill_blocktime'], r['amount_base_raw'],
                         'fill', r['fill_sig']))

    # Withdraws — explicit user pulls of escrow balance
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperWithdrawFundsEvent'"):
        p = bytes(r['payload'])
        if len(p) < 88: continue
        ob = _pk(p, WWFE_ORDERBOOK)
        if ob not in S2_ORDERBOOKS: continue
        wallet = _pk(p, WWFE_TRADER)
        sy = _u64(p, WWFE_AMOUNT_SY_OUT)
        yt = _u64(p, WWFE_AMOUNT_YT_OUT)
        if sy > 0:
            rows.append((wallet, ob, 'SY', r['blocktime'], -sy, 'withdraw', r['sig']))
        if yt > 0:
            rows.append((wallet, ob, 'YT', r['blocktime'], -yt, 'withdraw', r['sig']))

    # Cancel returns — both sides auto-refund via different paths:
    #   BuyYt cancel  → USX returns to user's wallet ATA (direct token transfer)
    #   SellYt cancel → YT returns to user's yield_position (via depositYtV2)
    # removeOfferEvent.amountOut is the actual amount removed (residual for
    # partial-then-cancel; full deposit for never-filled).
    ROE_AMOUNT_OUT_OFF = 161
    ROE_OFFER_IDX_OFF = 169
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'removeOfferEvent'"):
        p = bytes(r['payload'])
        if len(p) < 173: continue
        ob = _pk(p, 32)
        if ob not in S2_ORDERBOOKS: continue
        user = _pk(p, 0)
        slot = _u32(p, ROE_OFFER_IDX_OFF)
        amount_out = _u64(p, ROE_AMOUNT_OUT_OFF)
        if amount_out == 0:
            continue  # offer was fully consumed by fills — nothing to remove
        post = con.execute(
            "SELECT side FROM xpbook_offers "
            "WHERE orderbook = ? AND offer_idx = ? AND maker = ? "
            "AND post_blocktime <= ? "
            "ORDER BY post_blocktime DESC LIMIT 1",
            (ob, slot, user, r['blocktime'])).fetchone()
        if post is None: continue
        asset_kind = 'SY' if post['side'] == 'BuyYt' else 'YT' if post['side'] == 'SellYt' else None
        if asset_kind is None: continue
        rows.append((user, ob, asset_kind, r['blocktime'],
                     -amount_out, 'cancel', r['sig']))

    con.executemany(
        "INSERT OR IGNORE INTO xpbook_escrow_timeline "
        "(wallet, orderbook, asset_kind, event_blocktime, delta_raw, "
        " event_kind, event_sig) VALUES (?,?,?,?,?,?,?)", rows)
    print(f'  xpbook_escrow_timeline: {len(rows)} delta rows (S2 markets only)')


def _build_claims_inspection(con):
    rows = []
    for r in con.execute("SELECT sig, blocktime, payload FROM xpbook_events "
                          "WHERE event_name = 'wrapperWithdrawFundsEvent'"):
        p = bytes(r['payload'])
        if len(p) < 88: continue
        rows.append((r['sig'], r['blocktime'],
                     _pk(p, WWFE_TRADER), _pk(p, WWFE_ORDERBOOK),
                     _u64(p, WWFE_AMOUNT_PT_OUT),
                     _u64(p, WWFE_AMOUNT_YT_OUT),
                     _u64(p, WWFE_AMOUNT_SY_OUT)))
    con.executemany(
        "INSERT OR IGNORE INTO xpbook_claims "
        "(sig, blocktime, trader, orderbook, amount_pt_out_raw, "
        " amount_yt_out_raw, amount_sy_out_raw) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    print(f'  xpbook_claims: {len(rows)} withdraws indexed')


# (No estimation needed — actual fill cost from xpbook_fills is exact.)


def main():
    t0 = time.time()
    con = _db.conn()
    _init_schema(con)
    print('== transform_xpbook v3 ==')
    _build_offers_inspection(con)          # built first — needed for fill→maker lookup
    _build_fills(con)
    _build_claims_inspection(con)
    _build_wallet_flows(con)               # reads xpbook_fills for actual fill cost
    _build_escrow_timeline(con)            # for HOLD/YT walker TWAB integration

    # Verification on DDeKrHT + 5V9V
    print('\n=== Verification ===')
    for wallet_lbl, wallet in [
        ('DDeKrHT', 'DDeKrHTUyD3PeXjZsJyRtMYQ7mFfywXQHJBvMsbsAdcc'),
        ('5V9V',    '5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN'),
    ]:
        print(f'\n  {wallet_lbl}:')
        rows = con.execute("""
            SELECT substr(orderbook,1,14) ob,
                   usx_deposited_raw/1e6 dep_usx,
                   usx_sy_refunded_raw/1e6 sy_ref,
                   actual_fill_cost_raw/1e6 fill_cost,
                   escrow_residual_raw/1e6 residual,
                   yt_received_raw/1e6 yt_wdraw,
                   n_fills_as_maker || 'f ' || n_buy_posts || '/' || n_sell_posts ||
                   '/' || n_removes || '/' || n_withdraws as cnts
            FROM xpbook_wallet_orderbook_flow
            WHERE wallet = ? AND (usx_deposited_raw > 0 OR yt_received_raw > 0)
            ORDER BY dep_usx DESC""", (wallet,)).fetchall()
        print(f'    {"orderbook":16s}  {"BUY dep $":>12}  {"sy_ref":>10}  '
              f'{"fill cost":>10}  {"escrow residual":>16}  {"YT withdrawn":>14}  '
              f'{"fills, b/s/r/w":>16}')
        for r in rows:
            print(f'    {r["ob"]:16s}  ${r["dep_usx"]:>11,.2f}  '
                  f'${r["sy_ref"]:>9,.2f}  ${r["fill_cost"]:>9,.2f}  '
                  f'${r["residual"]:>15,.2f}  {r["yt_wdraw"]:>14,.2f}  '
                  f'{r["cnts"]:>16}')

    print(f'\n  Runtime: {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
