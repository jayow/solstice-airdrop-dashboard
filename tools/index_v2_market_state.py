"""Build the V2 (XPC1MM4d) CLMM tick-state timeline for one or more markets.

Method:
  1. Paginate getSignaturesForAddress(market_pda) to fetch every sig that ever
     touched the market.
  2. For each sig (in chronological order), getTransaction(jsonParsed) and
     scan its inner instructions for Anchor self-CPI events emitted BY the V2
     program (prefix e445a52e51cb9a1d || 8B event disc || borsh payload).
  3. Decode the three tick-state-affecting event types:
       - TradePtEvent       (9fe16051ffe3e9ae) → moves spot_tick
       - DepositLiquidityEvent (a95443aede8a107b) → mutates per-position L
       - WithdrawLiquidityEvent (d606a12dbf8e7cba) → mutates per-position L
     All other events (Buy/Sell {Pt,Yt} + Wrapper* variants) wrap one of the
     above via inner CPI in the same tx, so walking just these three is
     sufficient for tick-state reconstruction.
  4. Maintain in-memory per-market state:
       positions: { lp_position_pubkey: {"lo": u32, "hi": u32, "L": int} }
       spot_tick: int  (last known)
     and persist one v2_market_state_history row per state-mutating event.
     active_liquidity_at_spot = sum(p["L"] for p in positions if lo<=spot<hi)
     recomputed cheaply after each event.
  5. Write per-market progress to v2_market_walk_cursor.

After indexing, query helpers expose:
  - state_at(market, ts): (spot_tick, active_liquidity)
  - per-position state at any ts (rebuilt from the event sequence)

Usage:
  python tools/index_v2_market_state.py                       # both markets
  python tools/index_v2_market_state.py --market <PUBKEY>     # single market
  python tools/index_v2_market_state.py --max-workers 16
  python tools/index_v2_market_state.py --resume              # continue from cursor
"""
import os, sys, json, time, base58, struct, argparse, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "flares_estimator"))

from rpc_helper import rpc
from db import init as db_init, conn, txn

V2_PROG = "XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC"
MARKETS = {
    "USX-V2-Sep26":  "GesxMwfknVkVziqJKfGrNyhSoeBxdSEE6ZqpXk9Kbci8",
    "eUSX-V2-Sep26": "4yf98Xwht4z2K86VCHaS8tt5cbAxG529R7xVUs2dUf64",
}
# Per-market tick_space (read from MarketThree.configuration_options.tick_space).
# TradePtEvent.tick_after_trade is a SCALED tick index (raw_tick / tick_space).
# DepositLiquidityEvent / WithdrawLiquidityEvent.lower_tick / upper_tick are RAW ticks.
# We normalize everything to raw-tick units in the state machine.
TICK_SPACE = {
    "GesxMwfknVkVziqJKfGrNyhSoeBxdSEE6ZqpXk9Kbci8": 2500,   # USX-V2-Sep26
    "4yf98Xwht4z2K86VCHaS8tt5cbAxG529R7xVUs2dUf64": 2500,   # eUSX-V2-Sep26
}
EV_PREFIX = bytes.fromhex("e445a52e51cb9a1d")
DISC_TRADE_PT = bytes.fromhex("9fe16051ffe3e9ae")
DISC_DEPOSIT  = bytes.fromhex("a95443aede8a107b")
DISC_WITHDRAW = bytes.fromhex("d606a12dbf8e7cba")


# ---------------------------------------------------------------------------
# RPC walking
# ---------------------------------------------------------------------------

def fetch_all_sigs(addr: str, page_size: int = 1000, max_pages: int = 100,
                   force_refresh: bool = True, log_every: int = 5) -> list:
    """Paginate getSignaturesForAddress fully. Returns NEWEST-first list."""
    sigs, before = [], None
    page = 0
    while page < max_pages:
        page += 1
        p = [addr, {"limit": page_size}]
        if before:
            p[1]["before"] = before
        r = rpc("getSignaturesForAddress", p, force_refresh=force_refresh).get("result") or []
        if not r:
            break
        sigs.extend(r)
        before = r[-1]["signature"]
        if page % log_every == 0 or len(r) < page_size:
            oldest = datetime.fromtimestamp(r[-1].get("blockTime") or 0, UTC).strftime("%Y-%m-%d")
            print(f"    page {page}: {len(sigs):,} sigs (oldest={oldest})", flush=True)
        if len(r) < page_size:
            break
    return sigs


def fetch_tx(sig: str, force_refresh: bool = False, retries: int = 3) -> dict:
    """getTransaction(jsonParsed). Caches via rpc_helper (IMMUTABLE_METHODS).
    Retries on null responses (Helius transient nulls)."""
    for attempt in range(retries):
        r = rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 0}],
                force_refresh=(force_refresh and attempt == 0))
        tx = r.get("result")
        if tx:
            return tx
        time.sleep(0.3 * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Event decoding
# ---------------------------------------------------------------------------

def _resolve_pid(ix: dict, all_keys: list) -> str:
    pid = ix.get("programId") or ix.get("program")
    if pid:
        return pid
    idx = ix.get("programIdIndex")
    if idx is not None and idx < len(all_keys):
        return all_keys[idx]
    return None


def extract_v2_event_payloads(tx: dict) -> list:
    """Yield decoded event dicts for the three tick-affecting events found in
    this tx. We rely on inner instructions for the V2-program self-CPI events."""
    out = []
    meta = tx.get("meta") or {}
    inner_groups = meta.get("innerInstructions") or []
    if not inner_groups:
        return out
    msg = tx["transaction"]["message"]
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys", [])]
    loaded = meta.get("loadedAddresses", {}) or {}
    all_keys = keys + list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
    for grp in inner_groups:
        for ix in grp.get("instructions", []):
            pid = _resolve_pid(ix, all_keys)
            if pid != V2_PROG:
                continue
            d = ix.get("data")
            if not d:
                continue
            try:
                raw = base58.b58decode(d)
            except Exception:
                continue
            if len(raw) < 16 or raw[:8] != EV_PREFIX:
                continue
            disc = raw[8:16]
            payload = raw[16:]
            decoded = None
            if disc == DISC_TRADE_PT:
                decoded = decode_trade_pt(payload)
            elif disc == DISC_DEPOSIT:
                decoded = decode_deposit(payload)
            elif disc == DISC_WITHDRAW:
                decoded = decode_withdraw(payload)
            if decoded is not None:
                out.append(decoded)
    return out


def decode_trade_pt(p: bytes) -> dict:
    """TradePtEvent layout (verified against real tx):
      [0..32]    trader_address          pubkey
      [32..64]   market_address          pubkey
      [64..96]   token_sy_trader         pubkey
      [96..128]  token_pt_trader         pubkey
      [128]      swap_direction          u8 (enum: 0=PtToSy, 1=SyToPt)
      [129]      is_current_flash_swap   bool
      [130..138] amount_in               u64
      [138..146] amount_out              u64
      [146..154] total_fee               u64
      [154..162] treasury_fee            u64
      [162..166] tick_after_trade        u32
      [166..174] current_spot_price      f64
      [174..206] sy_exchange_rate Number(4×u64)
      [206..270] ModifiedTicks 4×u128 (fee_global before/after)
      [270..274] modified_ticks vec_len  u32
      [274..]    ModifiedTick × n  (36B each: u32 tick_idx + 2×u128 fee_growth)
    """
    if len(p) < 270:
        return None
    try:
        market    = base58.b58encode(p[32:64]).decode()
        swap_dir  = "PtToSy" if p[128] == 0 else "SyToPt"
        amount_in = int.from_bytes(p[130:138], "little")
        amount_out= int.from_bytes(p[138:146], "little")
        tick      = int.from_bytes(p[162:166], "little")
        return {
            "type": "TradePt",
            "market": market,
            "swap_dir": swap_dir,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "spot_tick_after": tick,
        }
    except Exception:
        return None


def decode_deposit(p: bytes) -> dict:
    """DepositLiquidityEvent fixed-header layout (verified):
      [0..32]    depositor                pubkey
      [32..64]   market_address           pubkey
      [64..96]   token_pt_src             pubkey
      [96..128]  token_sy_src             pubkey
      [128..136] amount_pt_in             u64
      [136..144] amount_sy_in             u64
      [144..152] delta_liquidity          u64
      [152..156] lower_tick               u32
      [156..160] upper_tick               u32
      [160..192] lp_position              pubkey
      [192..200] lp_balance_after         u64
      [200..208] tokens_owed_sy           u64
      [208..216] tokens_owed_pt           u64
      [216..]    farms + share_trackers + 2×u128 (variable)
    """
    if len(p) < 216:
        return None
    try:
        market    = base58.b58encode(p[32:64]).decode()
        delta_L   = int.from_bytes(p[144:152], "little")
        lower     = int.from_bytes(p[152:156], "little")
        upper     = int.from_bytes(p[156:160], "little")
        lp_pos    = base58.b58encode(p[160:192]).decode()
        return {
            "type": "Deposit",
            "market": market,
            "delta_liquidity": delta_L,
            "lower_tick": lower,
            "upper_tick": upper,
            "lp_position": lp_pos,
        }
    except Exception:
        return None


def decode_withdraw(p: bytes) -> dict:
    """WithdrawLiquidityEvent fixed-header layout (verified):
      [0..32]    withdrawer               pubkey
      [32..64]   market_address           pubkey
      [64..96]   token_pt_withdrawer      pubkey
      [96..128]  token_sy_withdrawer      pubkey
      [128..136] liquidity_to_remove      u64
      [136..144] amount_pt_out            u64
      [144..152] amount_sy_out            u64
      [152..160] new_lp_supply            u64
      [160..164] lower_tick               u32
      [164..168] upper_tick               u32
      [168..200] lp_position              pubkey
      [200..208] lp_balance_after         u64
      [208..]    fee_sy_collected + fee_pt_collected + farms + share_trackers + 2×u128
    """
    if len(p) < 208:
        return None
    try:
        market    = base58.b58encode(p[32:64]).decode()
        liq_rem   = int.from_bytes(p[128:136], "little")
        lower     = int.from_bytes(p[160:164], "little")
        upper     = int.from_bytes(p[164:168], "little")
        lp_pos    = base58.b58encode(p[168:200]).decode()
        return {
            "type": "Withdraw",
            "market": market,
            "delta_liquidity": liq_rem,    # to be subtracted
            "lower_tick": lower,
            "upper_tick": upper,
            "lp_position": lp_pos,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class MarketState:
    """Per-market in-memory tick state.

    positions[lp_position] = {"lo": int, "hi": int, "L": int}   # raw-tick units
    spot_tick                = last observed RAW spot tick (= scaled_idx × tick_space)

    TradePtEvent.tick_after_trade is a SCALED tick index (raw / tick_space) —
    we multiply by tick_space at apply() time. DepositLiquidityEvent and
    WithdrawLiquidityEvent already use raw ticks.
    """
    def __init__(self, tick_space: int):
        self.tick_space = tick_space
        self.positions: dict[str, dict] = {}
        self.spot_tick: int | None = None
        self.tick_observations: int = 0
        self.distinct_spot_ticks: set[int] = set()

    def apply(self, ev: dict):
        t = ev["type"]
        if t == "TradePt":
            new_tick = ev["spot_tick_after"] * self.tick_space
            if self.spot_tick != new_tick:
                self.tick_observations += 1
                self.distinct_spot_ticks.add(new_tick)
            self.spot_tick = new_tick
        elif t == "Deposit":
            lp = ev["lp_position"]
            pos = self.positions.get(lp)
            if pos is None:
                self.positions[lp] = {"lo": ev["lower_tick"], "hi": ev["upper_tick"],
                                      "L": ev["delta_liquidity"]}
            else:
                # Same lp_position can be re-deposited (same range — Exponent reuses position)
                pos["L"] += ev["delta_liquidity"]
        elif t == "Withdraw":
            lp = ev["lp_position"]
            pos = self.positions.get(lp)
            if pos is not None:
                pos["L"] -= ev["delta_liquidity"]
                if pos["L"] <= 0:
                    # Position drained — drop entirely
                    del self.positions[lp]

    def active_liquidity(self) -> int:
        """Sum L over all positions that span the current spot tick.
        Uniswap-V3 convention: [lower_tick, upper_tick) — lower-inclusive, upper-exclusive."""
        if self.spot_tick is None:
            return 0
        s = self.spot_tick
        return sum(p["L"] for p in self.positions.values() if p["lo"] <= s < p["hi"])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def walk_market(market_name: str, market_addr: str, max_workers: int,
                limit_pages: int | None = None, force_refresh: bool = True):
    print(f"\n=== {market_name} ({market_addr}) ===", flush=True)
    print(f"Fetching sigs...", flush=True)
    sigs = fetch_all_sigs(market_addr, max_pages=limit_pages or 100,
                          force_refresh=force_refresh)
    print(f"Total sigs: {len(sigs):,}", flush=True)

    # Fetch all txs in parallel (rpc_helper caches them; second run is fast)
    print(f"Fetching {len(sigs):,} txs ({max_workers} workers)...", flush=True)
    results = {}
    n_done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_tx, s["signature"]): s for s in sigs if not s.get("err")}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                tx = fut.result()
            except Exception:
                tx = None
            results[s["signature"]] = (s, tx)
            n_done += 1
            if n_done % 250 == 0 or n_done == len(futs):
                print(f"    {n_done}/{len(futs)} ({time.time()-t0:.0f}s)", flush=True)

    # Apply in chronological order (oldest first)
    sigs_chrono = sorted([s for s in sigs if not s.get("err")],
                         key=lambda x: (x.get("blockTime") or 0, x["signature"]))

    tick_space = TICK_SPACE.get(market_addr, 1)
    state = MarketState(tick_space=tick_space)
    state_rows = []  # (market, ts, slot, sig, event_type, spot_tick, active_L, lp_pos, delta_L, lower, upper, swap_dir, amt_in, amt_out)
    n_events = 0
    n_missing = 0
    for s in sigs_chrono:
        sig = s["signature"]
        if sig not in results:
            continue
        _, tx = results[sig]
        if not tx:
            n_missing += 1
            continue
        evs = extract_v2_event_payloads(tx)
        for ev in evs:
            if ev.get("market") != market_addr:
                continue
            state.apply(ev)
            n_events += 1
            spot = state.spot_tick if state.spot_tick is not None else 0
            active_L = state.active_liquidity()
            ts   = s.get("blockTime") or 0
            slot = s.get("slot") or 0
            if ev["type"] == "TradePt":
                state_rows.append((market_addr, ts, slot, sig, "TradePt",
                                    spot, str(active_L), None, None, None, None,
                                    ev["swap_dir"], str(ev["amount_in"]), str(ev["amount_out"])))
            elif ev["type"] == "Deposit":
                state_rows.append((market_addr, ts, slot, sig, "Deposit",
                                    spot, str(active_L),
                                    ev["lp_position"], "+" + str(ev["delta_liquidity"]),
                                    ev["lower_tick"], ev["upper_tick"], None, None, None))
            elif ev["type"] == "Withdraw":
                state_rows.append((market_addr, ts, slot, sig, "Withdraw",
                                    spot, str(active_L),
                                    ev["lp_position"], "-" + str(ev["delta_liquidity"]),
                                    ev["lower_tick"], ev["upper_tick"], None, None, None))

    print(f"  events applied: {n_events:,}  (txs missing: {n_missing}, "
          f"distinct spot ticks observed: {len(state.distinct_spot_ticks)}, "
          f"open positions at end: {len(state.positions)})", flush=True)

    # Persist
    db_init()
    with txn() as c:
        c.execute("DELETE FROM v2_market_state_history WHERE market = ?", (market_addr,))
        c.executemany(
            "INSERT INTO v2_market_state_history "
            "(market, ts, slot, sig, event_type, spot_tick, active_liquidity, "
            " lp_position, delta_liquidity, lower_tick, upper_tick, "
            " swap_dir, amount_in, amount_out) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            state_rows)
        c.execute(
            "INSERT OR REPLACE INTO v2_market_walk_cursor "
            "(market, last_sig, last_ts, n_sigs_walked, n_state_updates, refreshed_at) "
            "VALUES (?,?,?,?,?,strftime('%s','now'))",
            (market_addr, sigs_chrono[-1]["signature"] if sigs_chrono else None,
             sigs_chrono[-1].get("blockTime") if sigs_chrono else None,
             len(sigs_chrono), len(state_rows)))
    print(f"  persisted {len(state_rows):,} state-history rows", flush=True)
    return {
        "market": market_addr,
        "market_name": market_name,
        "n_sigs_walked": len(sigs_chrono),
        "n_state_updates": len(state_rows),
        "tick_observations": state.tick_observations,
        "distinct_spot_ticks": len(state.distinct_spot_ticks),
        "open_positions_end": len(state.positions),
        "spot_tick_end": state.spot_tick,
        "active_L_end": state.active_liquidity(),
    }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def state_at_ts(market: str, ts: int) -> dict:
    """Latest (spot_tick, active_liquidity) for `market` at-or-before `ts`."""
    c = conn()
    row = c.execute(
        "SELECT ts, sig, event_type, spot_tick, active_liquidity, lp_position, "
        "       delta_liquidity, lower_tick, upper_tick "
        "FROM v2_market_state_history WHERE market = ? AND ts <= ? "
        "ORDER BY ts DESC, slot DESC LIMIT 1",
        (market, int(ts))
    ).fetchone()
    if not row:
        return None
    return {
        "ts": row["ts"], "sig": row["sig"], "event_type": row["event_type"],
        "spot_tick": row["spot_tick"],
        "active_liquidity": int(row["active_liquidity"]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", help="single market PDA; default = both V2 markets")
    ap.add_argument("--max-workers", type=int, default=16)
    ap.add_argument("--limit-pages", type=int, default=None,
                    help="defensive cap on sig pagination (1000 sigs/page)")
    ap.add_argument("--no-force-refresh-sigs", action="store_true",
                    help="default we refresh sig list to catch new sigs; tx body is "
                         "always cache-only (finalized data, immutable)")
    args = ap.parse_args()

    targets = []
    if args.market:
        name = next((k for k, v in MARKETS.items() if v == args.market), args.market[:8])
        targets.append((name, args.market))
    else:
        for n, a in MARKETS.items():
            targets.append((n, a))

    summary = []
    for name, addr in targets:
        s = walk_market(name, addr, args.max_workers,
                        limit_pages=args.limit_pages,
                        force_refresh=not args.no_force_refresh_sigs)
        summary.append(s)
    print("\n=== SUMMARY ===")
    for s in summary:
        print(json.dumps(s, default=str))


if __name__ == "__main__":
    main()
