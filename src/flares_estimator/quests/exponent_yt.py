"""
S2_EXPONENT_YIELD_USX_JUN26 (30x), S2_EXPONENT_YIELD_EUSX_JUN26 (15x).

Per-quest, per-wallet ELT for Exponent YT positions.

Raw on-chain proof of YT exposure during S2:
  1. Find every YieldPosition PDA (size 168, authority @ offset 8 = wallet) — current.
  2. For each PDA on a Solstice-incentivized market, get its full sig history.
  3. Walk each sig and parse the YT mint (offset 40 of market) balance change in
     the market vault → derives ytDelta from user perspective.
  4. Compute carry-in (sum of pre-S2 ytDeltas) + S2-window event timeline.
  5. Integrate yt × duration × multiplier × calibrated rate.

Closed-during-S2 positions: PDA still has size 168 even after sellYt; only
yt_amount field is 0. Sig history is preserved. We capture them.

Calibration anchor (verified 2026-05-09): user wallet
5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN — 2,519.38 YT × 1023.55 = daily
2,578,542 flares per dashboard. So per-YT-per-day rate (mult included) = 1023.55
for USX-Jun26. eUSX-Jun26 estimated half (15× vs 30×) ≈ 511.78.

CAVEAT: this rate is the OPEN-position emission. Closed positions emitted at a
lower historical rate (Solstice rate decays approaching maturity). Friend wallet
calibration (closed Apr 14) implies ~34 flares/YT/day for that period — about
30× lower than open. We currently use the open rate uniformly; closed-position
contribution is over-estimated. A future per-segment decay model would correct it.
"""
import os, sys, time, base64, base58, struct
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rpc_helper import rpc
from .base import QuestExtractor, S2_START_TS, S2_END_TS, load_quest_cache

EXPONENT_CORE = "ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7"

# Solstice formula (verified 2026-05-11 on user wallet 5V9VwuVq…LojKN):
#   daily_flares = yt_amount × multiplier × 1.0
# Exact match: 85,951.41 × 30 = 2,578,542.3 (dashboard: 2,578,542)
#
# Inclusion rule (per indexing-exponent-yt memory, empirically validated):
# a position counts only if its newest pre-S2 sig was NOT a closing
# instruction. Both V1 and V2 struct shapes can be valid; the discriminator
# is sig-walk, not struct disc. On-chain `yt_amount` is stale on V1 after
# withdraws so reading it alone over-counts.
BASE_RATE_PER_YT_PER_DAY_PER_MULT = 1.0

MARKETS = {
    "BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm": {
        "label": "USX-Jun26", "mult": 30, "quest": "S2_EXPONENT_YIELD_USX_JUN26",
    },
    "rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP": {
        "label": "eUSX-Jun26", "mult": 15, "quest": "S2_EXPONENT_YIELD_EUSX_JUN26",
    },
}

# Two account types Exponent v1 uses for user positions:
#   disc 69f125c8e002fc5a size 168 → v1 YieldPosition: market@40, yt@128 (u64/1e6), emit@112
#   disc e35c92311d55475e size 124/164/204 → v2/alt position: yield_pool@40, yt@72 (u64/1e6)
#     The "yield_pool" at offset 40 is the market's own offset-104 reference.

V1_DISC = "69f125c8e002fc5a"
V2_DISC = "e35c92311d55475e"

# Per-market lookup of (market@40 alias, market@104 alias) for cross-referencing
# v2 positions back to markets.
_market_xref_cache: dict = {}
_yt_mint_cache: dict = {}


def _get_yt_mint(market_pk: str):
    """YT mint = offset 40 of market account (verified empirically on sellYt traces)."""
    if market_pk in _yt_mint_cache: return _yt_mint_cache[market_pk]
    r = rpc("getAccountInfo", [market_pk, {"encoding": "base64"}])
    v = r.get("result", {}).get("value")
    if not v: return None
    d = base64.b64decode(v["data"][0])
    if len(d) < 72: return None
    mint = base58.b58encode(d[40:72]).decode()
    _yt_mint_cache[market_pk] = mint
    return mint


def _build_market_xref() -> dict:
    """Build (alias_pubkey → market_pubkey) for both offset-40 and offset-104
    references in each market account. This lets us map v2/alt positions back
    to a known Solstice market."""
    global _market_xref_cache
    if _market_xref_cache: return _market_xref_cache
    xref = {}
    for m_pk in MARKETS:
        r = rpc("getAccountInfo", [m_pk, {"encoding": "base64"}])
        v = r.get("result", {}).get("value")
        if not v: continue
        d = base64.b64decode(v["data"][0])
        if len(d) >= 72:
            off40 = base58.b58encode(d[40:72]).decode()
            xref[off40] = m_pk
        if len(d) >= 136:
            off104 = base58.b58encode(d[104:136]).decode()
            xref[off104] = m_pk
        xref[m_pk] = m_pk  # direct market pubkey
    _market_xref_cache = xref
    return xref


def _get_yieldpositions(wallet: str) -> list:
    """All Exponent program accounts where authority @ offset 8 == wallet.

    Captures BOTH account types:
      - v1 YieldPosition (disc 69f125c8, size 168): market@40, yt@128, emit@112
      - v2/alt position  (disc e35c92,   size 124/164/204): yield_pool@40, yt@72

    Maps v2 positions to a market via offset-40 alias lookup (which equals the
    market's own offset-104 reference). Returns a unified list with market label
    and yt amount per position.
    """
    r = rpc("getProgramAccounts", [EXPONENT_CORE, {
        "encoding": "base64",
        "filters": [{"memcmp": {"offset": 8, "bytes": wallet}}],
    }], timeout=60)
    xref = _build_market_xref()
    out = []
    for a in (r.get("result") or []):
        try:
            d = base64.b64decode(a["account"]["data"][0])
        except Exception: continue
        if len(d) < 72: continue
        disc = d[:8].hex()
        size = len(d)
        off40 = base58.b58encode(d[40:72]).decode()

        if disc == V1_DISC and size == 168:
            # V1 YieldPosition — market PDA stored DIRECTLY at offset 40.
            market = off40
            yt_amount = struct.unpack("<Q", d[128:136])[0] / 1e6
            kind = "V1"
        elif disc == V2_DISC and size in (124, 164, 204):
            # V2 position — offset 40 is an alias to the yield_pool; resolve
            # via the market xref (which includes self-mappings, offset-40
            # aliases, and offset-104 aliases).
            market = xref.get(off40)
            if market is None: continue
            yt_amount = struct.unpack("<Q", d[72:80])[0] / 1e6
            kind = "V2"
        else:
            continue

        out.append({
            "pubkey": a["pubkey"], "market": market,
            "yt_amount_now": yt_amount, "kind": kind,
        })
    return out


def _walk_pda_sigs(pda: str, max_pages: int = 10) -> list:
    sigs = []; before = None
    for _ in range(max_pages):
        params = [pda, {"limit": 1000, **({"before": before} if before else {})}]
        r = rpc("getSignaturesForAddress", params)
        page = r.get("result") or []
        if not page: break
        sigs.extend(page)
        before = page[-1]["signature"]
        if len(page) < 1000: break
    sigs.sort(key=lambda s: s.get("blockTime") or 0)
    return sigs


def _yt_delta_for_signer(tx: dict, signer: str, yt_mint: str):
    """Return user-perspective YT delta from a tx; same logic as enrich_yt_deltas."""
    meta = tx.get("meta", {}) or {}
    pre = meta.get("preTokenBalances", []) or []
    post = meta.get("postTokenBalances", []) or []
    by_idx_pre = {p["accountIndex"]: p for p in pre}
    by_idx_post = {p["accountIndex"]: p for p in post}
    market_vault_delta = 0.0
    user_owned_delta = 0.0
    for idx in set(by_idx_pre) | set(by_idx_post):
        a = by_idx_pre.get(idx, {})
        b = by_idx_post.get(idx, {})
        mint = a.get("mint") or b.get("mint")
        if mint != yt_mint: continue
        pa = float((a.get("uiTokenAmount", {}) or {}).get("uiAmount") or 0)
        pb = float((b.get("uiTokenAmount", {}) or {}).get("uiAmount") or 0)
        delta = pb - pa
        if abs(delta) < 1e-9: continue
        owner = a.get("owner") or b.get("owner") or ""
        if owner == signer:
            if abs(delta) > abs(user_owned_delta):
                user_owned_delta = delta
        else:
            if abs(delta) > abs(market_vault_delta):
                market_vault_delta = delta
    if user_owned_delta != 0: return user_owned_delta
    if market_vault_delta != 0: return market_vault_delta
    return None


def _build_yt_timeline(wallet: str) -> dict:
    """For every Solstice-market YieldPosition PDA, build (ts, yt_balance) timeline.

    Two paths:
      (a) Sufficient PDA history: walk each tx, parse YT mint balance change from
          market vault, build event-anchored timeline.
      (b) Insufficient history (sparse sigs OR all deltas unparseable BUT current
          yt_amount > 0): fall back to current-state anchor — assume current YT
          held throughout the S2 window. Less accurate for mid-S2 entrants, but
          captures emission for currently-emitting positions whose history isn't
          fully visible to free-tier RPC.
    """
    positions = _get_yieldpositions(wallet)
    by_market = {}
    last_slot = 0
    yt_mints = {}
    for m_pk, cfg in MARKETS.items():
        yt_mints[m_pk] = _get_yt_mint(m_pk)

    for pos in positions:
        market = pos["market"]
        if market not in MARKETS: continue
        yt_mint = yt_mints.get(market)
        if not yt_mint: continue
        sigs = _walk_pda_sigs(pos["pubkey"])
        pre_s2_delta = 0.0
        events = []
        n_extractable = 0
        for s in sigs:
            ts = s.get("blockTime") or 0
            if not ts: continue
            slot = s.get("slot", 0)
            last_slot = max(last_slot, slot)
            r = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            tx = r.get("result")
            if not tx: continue
            d = _yt_delta_for_signer(tx, wallet, yt_mint)
            if d is None: continue
            n_extractable += 1
            if ts < S2_START_TS:
                pre_s2_delta += d
            else:
                events.append([ts, d])

        # Decide: did we get a usable history?
        carry_in = max(0.0, pre_s2_delta)
        history_usable = n_extractable >= 1 and (carry_in > 0 or events)
        end_ts = min(int(time.time()), S2_END_TS)

        if history_usable:
            timeline = [[S2_START_TS, carry_in]]
            running = carry_in
            for ts, d in events:
                running = max(0.0, running + d)
                timeline.append([ts, running])
            timeline.append([end_ts, running])
            method = "history"
        else:
            # Fallback: anchor at current YT for full S2 window. Conservative for
            # mid-S2 entrants but matches what feature_extractor's calibrated
            # current-state path produces.
            cur = pos["yt_amount_now"]
            timeline = [[S2_START_TS, cur], [end_ts, cur]]
            method = "current_state_fallback"

        by_market.setdefault(market, []).append({
            "pubkey": pos["pubkey"], "timeline": timeline,
            "current_yt": pos["yt_amount_now"], "method": method,
            "is_emitting": pos.get("is_emitting", False),
        })
    return {"positions_by_market": by_market, "_watermark": {"slot": last_slot, "ts": int(time.time())}}


def _integrate_yt(timeline: list, rate_per_yt_per_day: float, end_ts: int) -> float:
    """Integrate YT × time over [S2_START_TS, end_ts]. Clamping at integration
    time (not extract time) means corrections to S2_START_TS apply retroactively
    without forcing a re-extract."""
    flares = 0.0
    for i in range(len(timeline) - 1):
        t0, yt = timeline[i]
        t1, _ = timeline[i + 1]
        if t0 < S2_START_TS: t0 = S2_START_TS
        if t1 > end_ts: t1 = end_ts
        if t1 <= t0: continue
        flares += yt * rate_per_yt_per_day * (t1 - t0) / 86400.0
    # If carry-in balance present and timeline's first point >= S2_START, no extra
    # time to extend. But if the cache's first point is later than the corrected
    # S2_START_TS (cache built with an older S2 epoch), back-extend at the
    # carry-in balance (timeline[0][1]) to the corrected epoch.
    if timeline:
        first_t, first_b = timeline[0]
        if first_t > S2_START_TS and first_b > 0:
            flares += first_b * rate_per_yt_per_day * (first_t - S2_START_TS) / 86400.0
        # Tail: forward-extend the last observed YT amount to end_ts. Without
        # this, flares are frozen at the cache extract time — Solstice computes
        # in real time, so transforms re-run with a newer end_ts must include
        # the gap.
        last_t, last_yt = timeline[-1]
        if last_t < end_ts and last_yt > 0:
            flares += last_yt * rate_per_yt_per_day * (end_ts - last_t) / 86400.0
    return flares


# Wrapper-level instructions that express the user's intent on a YieldPosition.
# A "closing" intent burns/removes YT; an "opening" one adds it. The on-chain
# yt_amount field on V1 LP-residue positions does NOT zero on close, so we
# must classify by intent, not by the field value.
_CLOSING_IXS = {
    "WrapperSellYt", "WrapperRedeemYt",
    "WrapperWithdrawLiquidity", "WrapperWithdrawLiquidityBase",
    "WrapperRemoveLiquidity", "WrapperRemoveLiquidityBase",
}
_OPENING_IXS = {
    "WrapperBuyYt", "WrapperMintYt",
    "WrapperProvideLiquidity", "WrapperProvideLiquidityBase",
    "WrapperAddLiquidity", "WrapperAddLiquidityBase",
}


def _has_wrapper_buy_yt(sig_info: dict) -> bool:
    """True if the tx invoked WrapperBuyYt or WrapperMintYt at the wrapper
    level. Used to distinguish active V1 YT bets from V1 LP-residue (which
    only see WrapperProvideLiquidity / direct MarketDepositLp)."""
    try:
        r = rpc("getTransaction", [sig_info["signature"],
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        tx = r.get("result")
        if not tx: return False
        for ln in (tx.get("meta", {}).get("logMessages") or []):
            if "Program log: Instruction:" not in ln: continue
            nm = ln.split("Instruction:", 1)[1].strip().split()[0]
            if nm in ("WrapperBuyYt", "WrapperMintYt"):
                return True
    except Exception: pass
    return False


def _classify_position_at_s2(sigs: list) -> str:
    """Walk all pre-S2 sigs of the position. Return:
      'closed' — newest opening/closing intent is a closing one (user exited).
      'open'   — newest opening/closing intent is an opening one OR no intents
                 are visible (default trust the on-chain yt_amount).

    Scans every tx's logs for a wrapper-level instruction name; classifies by
    intent. The newest-timestamp intent wins. (Sub-instructions like
    MarketDepositLp / MarketWithdrawLp aren't user intents — they're inner
    invocations and are ignored.)"""
    pre_s2 = [s for s in sigs if (s.get("blockTime") or 0) < S2_START_TS]
    if not pre_s2: return "open"

    latest_intent_ts = -1
    latest_intent_kind = None
    for s in pre_s2:
        ts = s.get("blockTime") or 0
        try:
            r = rpc("getTransaction", [s["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            tx = r.get("result")
            if not tx: continue
            for ln in (tx.get("meta", {}).get("logMessages") or []):
                if "Program log: Instruction:" not in ln: continue
                nm = ln.split("Instruction:", 1)[1].strip().split()[0]
                kind = None
                if nm in _CLOSING_IXS: kind = "closed"
                elif nm in _OPENING_IXS: kind = "open"
                if kind and ts >= latest_intent_ts:
                    latest_intent_ts = ts
                    latest_intent_kind = kind
        except Exception:
            continue
    return latest_intent_kind or "open"


def _build_yt_positions_with_carry(wallet: str) -> dict:
    """Enumerate the wallet's YT position PDAs and decide each one's S2 carry-in.

    Per the empirical rule (verified for wallet 5V9V against Solstice's own
    numbers): the on-chain `yt_amount` field on a position PDA can be STALE.
    V1 LP-residue positions do not zero `yt_amount` when the user withdraws,
    so reading it alone over-counts. The actual discriminator is whether the
    NEWEST PRE-S2 SIG on the position is a closing instruction
    (WrapperWithdrawLiquidity / WrapperSellYt / WrapperRedeemYt / etc.) — if
    so, the user exited pre-S2 and the position carries 0 YT into S2.

    Returns:
      {
        "positions_by_market": {
          market_pk: [
            {"pubkey": ..., "kind": "V1"|"V2", "yt_amount_now": float,
             "carry_in_yt": float, "newest_pre_s2_closing": bool,
             "timeline": [[ts, yt], ...]}
          ]
        },
        "_watermark": {...}
      }
    """
    positions = _get_yieldpositions(wallet)
    by_market = {}
    last_slot = 0
    end_ts = min(int(time.time()), S2_END_TS)

    for pos in positions:
        market = pos["market"]
        if market not in MARKETS: continue
        sigs = _walk_pda_sigs(pos["pubkey"])
        if sigs:
            last_slot = max(last_slot, max((s.get("slot") or 0) for s in sigs))
        state_at_s2 = _classify_position_at_s2(sigs)
        in_s2_sigs = [s for s in sigs if (s.get("blockTime") or 0) >= S2_START_TS]

        # V1 positions: distinguish active V1 YT from LP-residue. The V1
        # YieldPosition has yt_amount at offset 128 that gets WRITTEN when LP is
        # provided (residue from PT/SY strip) but does NOT get reset on LP-burn
        # at the market level — so it's structurally stale after any LP exit.
        # The only way V1 yt_amount represents a real YT bet is if the user
        # ever called WrapperBuyYt against this position. Otherwise it's pure
        # LP-residue and the YT exposure is already captured by the LP quests.
        if pos["kind"] == "V1":
            has_active_yt = any(
                _has_wrapper_buy_yt(s) for s in sigs
            ) if sigs else False
            if not has_active_yt:
                carry_in = 0.0
                timeline = [[S2_START_TS, 0.0], [end_ts, 0.0]]
                state_at_s2 = "v1_lp_residue"
            elif state_at_s2 == "closed":
                carry_in = 0.0
                timeline = [[S2_START_TS, 0.0], [end_ts, 0.0]]
            else:
                cur_yt = float(pos["yt_amount_now"])
                carry_in = cur_yt  # V2-style: take current and walk events
                timeline = [[S2_START_TS, carry_in], [end_ts, carry_in]]
        elif state_at_s2 == "closed":
            # User exited pre-S2; on-chain yt_amount is stale. Zero credit.
            carry_in = 0.0
            timeline = [[S2_START_TS, 0.0], [end_ts, 0.0]]
        else:
            # Position carried into S2. Derive carry-in by walking S2 sigs to
            # collect token deltas, then subtracting their net from the current
            # on-chain yt_amount. The on-chain field is what the wallet has NOW
            # (post all events), so:
            #   carry_in = current_yt - sum(s2_deltas)
            # Walking forward from carry_in then naturally arrives back at
            # current_yt as the last point. This is the same carry-in pattern
            # used by transform_kamino.transform_wallet.
            cur_yt = float(pos["yt_amount_now"])
            yt_mint = _get_yt_mint(market)
            s2_deltas = []  # list of (ts, delta)
            for s in in_s2_sigs:
                ts = s.get("blockTime") or 0
                try:
                    r = rpc("getTransaction", [s["signature"],
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                    tx = r.get("result")
                    if not tx: continue
                    delta = _yt_delta_for_signer(tx, wallet, yt_mint) if yt_mint else None
                    if delta is None: continue
                    s2_deltas.append((ts, delta))
                except Exception:
                    continue
            s2_deltas.sort(key=lambda x: x[0])
            net_s2 = sum(d for _, d in s2_deltas)
            carry_in = max(0.0, cur_yt - net_s2)
            timeline = [[S2_START_TS, carry_in]]
            running = carry_in
            for ts, d in s2_deltas:
                running = max(0.0, running + d)
                timeline.append([ts, running])
            timeline.append([end_ts, running])

        by_market.setdefault(market, []).append({
            "pubkey": pos["pubkey"], "kind": pos["kind"],
            "yt_amount_now": pos["yt_amount_now"],
            "carry_in_yt": carry_in,
            "state_at_s2": state_at_s2,
            "timeline": timeline,
        })

    return {"positions_by_market": by_market,
            "_watermark": {"slot": last_slot, "ts": int(time.time())},
            "schema": "yt_position_v2"}


class ExponentYTExtractor(QuestExtractor):
    QUEST_CODE = ("S2_EXPONENT_YIELD_USX_JUN26", "S2_EXPONENT_YIELD_EUSX_JUN26")
    SHARED_CACHE_KEY = "S2_EXPONENT_YT"

    def extract(self, wallet: str) -> dict:
        return _build_yt_positions_with_carry(wallet)

    def looks_empty(self, raw: dict) -> bool:
        return not (raw.get("positions_by_market") or {})

    def quick_validate(self, wallet: str) -> bool:
        # Source B: does the wallet have ANY Exponent program account at
        # owner@offset=8? If yes, it has at least one YieldPosition PDA. This
        # uses getProgramAccounts on the Exponent program, a different code
        # path than the extractor's per-position sig walk.
        try:
            r = rpc("getProgramAccounts", [EXPONENT_CORE, {
                "encoding": "base64",
                "filters": [{"memcmp": {"offset": 8, "bytes": wallet}}],
                "dataSlice": {"offset": 0, "length": 8},
            }], timeout=30, force_refresh=True)
            return bool(r.get("result"))
        except Exception:
            return False

    def transform(self, raw: dict, now_ts: int) -> dict:
        out = {q: 0.0 for q in self.QUEST_CODE}
        end_ts = min(now_ts, S2_END_TS)
        for market, positions in (raw.get("positions_by_market") or {}).items():
            cfg = MARKETS.get(market)
            if not cfg: continue
            rate = BASE_RATE_PER_YT_PER_DAY_PER_MULT * cfg["mult"]
            for p in positions:
                # Trust the timeline. _build_yt_positions_with_carry already
                # zeroed it out for positions the user exited pre-S2.
                tl = p.get("timeline") or []
                if not tl: continue
                f = _integrate_yt(tl, rate, end_ts)
                out[cfg["quest"]] += f
        return out
