"""
S2_HOLD_USX_DAILY (10x), _1MO (6x), _3MO (15x).

All three derive from the same raw data: the wallet's USX ATA balance timeline
over the S2 window. Extract once, transform thrice.

Raw schema:
  {
    "atas": ["<ata_pubkey>", ...],
    "timeline": [[ts, balance_uiAmount], ...],  # sorted ascending; covers
                 # carry-in at S2_START + every change inside S2 + endpoint at extracted_at
    "_watermark": {"slot": <last_slot_seen>, "ts": <ts of latest sig>}
  }
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rpc_helper import rpc
from .base import QuestExtractor, S2_START_TS, S2_END_TS, load_quest_cache

USX_MINT = "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"

# Associated Token Account program — used to deterministically derive the
# canonical ATA for (wallet, mint). The canonical ATA exists at a fixed
# address whether or not the account is currently open; its signature history
# survives account closure, so we can always rebuild HOLD flares from it.
try:
    from solders.pubkey import Pubkey as _Pubkey
    _TOKEN_PROG = _Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    _ATOK_PROG  = _Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    def _canonical_ata(wallet: str, mint: str) -> str | None:
        try:
            pda, _ = _Pubkey.find_program_address(
                [bytes(_Pubkey.from_string(wallet)), bytes(_TOKEN_PROG), bytes(_Pubkey.from_string(mint))],
                _ATOK_PROG)
            return str(pda)
        except Exception:
            return None
except Exception:
    def _canonical_ata(wallet: str, mint: str) -> str | None:
        return None


def _list_atas(wallet: str, mint: str) -> list:
    r = rpc("getTokenAccountsByOwner",
             [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    return [acc["pubkey"] for acc in (r.get("result", {}).get("value", []) or [])]


def _walk_ata_sigs(ata: str, max_pages: int = 10) -> list:
    """Return (sig_meta, ...) ordered ascending by blockTime."""
    sigs = []; before = None
    for _ in range(max_pages):
        params = [ata, {"limit": 1000, **({"before": before} if before else {})}]
        r = rpc("getSignaturesForAddress", params)
        page = r.get("result") or []
        if not page: break
        sigs.extend(page)
        before = page[-1]["signature"]
        if len(page) < 1000: break
    sigs.sort(key=lambda s: s.get("blockTime") or 0)
    return sigs


def _post_balance(sig: str, ata: str):
    """Return (balance_uiAmount, slot) after the sig. If the account was closed
    in this tx (present in preTokenBalances, absent from post), return (0, slot)
    so the timeline correctly records the close-to-zero event."""
    r = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    tx = r.get("result")
    if not tx: return None
    msg = tx["transaction"]["message"]
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys", [])]
    if ata not in keys: return None
    idx = keys.index(ata)
    meta = tx.get("meta", {}) or {}
    post = next((b for b in (meta.get("postTokenBalances", []) or [])
                  if b.get("accountIndex") == idx), None)
    if post:
        return float(post.get("uiTokenAmount", {}).get("uiAmount") or 0), tx.get("slot", 0)
    pre = next((b for b in (meta.get("preTokenBalances", []) or [])
                 if b.get("accountIndex") == idx), None)
    if pre:
        # Account closed in this tx — record the zero-out so we don't carry the
        # prior balance forward across the close event.
        return 0.0, tx.get("slot", 0)
    return None


def _build_timeline(wallet: str, mint: str, now_ts: int, prior_atas: list | None = None) -> dict:
    """Walk all ATAs for (wallet, mint), build a single combined balance timeline.

    `prior_atas`: previously-observed ATAs from cache. Unioned with the current
    `getTokenAccountsByOwner` result so that CLOSED ATAs still get walked —
    their signature history is preserved on the validator even after the
    account is gone, so we can still reconstruct the flares they earned before
    closure. Without this, a wallet that closed its USX ATA would have its
    entire HOLD history wiped on the next refresh."""
    current_atas = _list_atas(wallet, mint)
    end_ts = min(now_ts, S2_END_TS)
    # Always include the canonical ATA (deterministic from wallet+mint) — its
    # signature history is reachable even after closure, so a wallet that
    # closed-then-cleaned-up still gets its prior holding flares back.
    canon = _canonical_ata(wallet, mint)
    seed = (prior_atas or []) + current_atas + ([canon] if canon else [])
    atas = list(dict.fromkeys(a for a in seed if a))

    # If wallet has never had an ATA at all, no balance ever existed
    if not atas:
        return {"atas": [], "timeline": [[S2_START_TS, 0.0], [end_ts, 0.0]],
                "_watermark": {"slot": 0, "ts": end_ts}}

    # Merge balances across all ATAs at each event
    per_ata_segments = {}
    last_slot = 0
    for ata in atas:
        sigs = _walk_ata_sigs(ata)
        if not sigs: continue
        # carry_in = balance just before S2_START
        carry_in = 0.0
        pre = [s for s in sigs if (s.get("blockTime") or 0) < S2_START_TS]
        if pre:
            r = _post_balance(pre[-1]["signature"], ata)
            carry_in = (r or (0.0, 0))[0]
        segs = [(S2_START_TS, carry_in)]
        in_s2 = [s for s in sigs if S2_START_TS <= (s.get("blockTime") or 0) <= end_ts]
        for s in in_s2:
            ts = s.get("blockTime") or 0
            r = _post_balance(s["signature"], ata)
            if r is None: continue
            bal, slot = r
            last_slot = max(last_slot, slot)
            if ts <= segs[-1][0]: continue
            segs.append((ts, bal))
        per_ata_segments[ata] = segs

    # Build the union timeline by sweeping all event timestamps
    all_ts = sorted({S2_START_TS, end_ts} | {ts for segs in per_ata_segments.values() for ts, _ in segs})
    timeline = []
    for t in all_ts:
        total = 0.0
        for segs in per_ata_segments.values():
            # find last segment with ts <= t
            last = 0.0
            for ts, b in segs:
                if ts <= t: last = b
                else: break
            total += last
        if not timeline or total != timeline[-1][1] or t == end_ts:
            timeline.append([t, total])

    return {"atas": atas, "timeline": timeline,
            "_watermark": {"slot": last_slot, "ts": end_ts}}


def _integrate_twab(timeline: list, mult: int, usd_per_token, end_ts: int) -> float:
    """Daily TWAB flares = Σ balance × usd_per_token × mult × Δt_days. Standard for HOLD.

    `usd_per_token` may be a float (constant, e.g. USX = $1) or a callable
    `peg_fn(ts) → float` (e.g. eUSX peg varies over time). When a callable is
    passed, the segment-midpoint peg is used — accurate to second order for
    smoothly-varying peg curves.

    Extends the last observed balance forward to end_ts. Without this tail
    extension, a fresh transform call with a newer now_ts than the extract
    time would silently undercount the gap."""
    flares = 0.0
    if not timeline: return 0.0
    is_callable = callable(usd_per_token)
    def _usd(t0, t1):
        if is_callable: return usd_per_token((t0 + t1) // 2)
        return usd_per_token
    for i in range(len(timeline) - 1):
        t0, b0 = timeline[i]
        t1, _ = timeline[i + 1]
        if t0 < S2_START_TS: t0 = S2_START_TS
        if t1 > end_ts: t1 = end_ts
        if t1 <= t0: continue
        flares += b0 * _usd(t0, t1) * mult * (t1 - t0) / 86400.0
    last_t, last_b = timeline[-1]
    if last_t < end_ts and last_b > 0:
        flares += last_b * _usd(last_t, end_ts) * mult * (end_ts - last_t) / 86400.0
    first_t, first_b = timeline[0]
    if first_t > S2_START_TS and first_b > 0:
        flares += first_b * _usd(S2_START_TS, first_t) * mult * (first_t - S2_START_TS) / 86400.0
    return flares


def _integrate_qualified_bonus(timeline: list, min_bal: float, qualify_days: int,
                                mult: int, usd_per_token, end_ts: int) -> float:
    """1MO/3MO bonus: scales with ACTUAL balance once the wallet has held ≥min_bal
    continuously for at least `qualify_days`. After qualification, every additional
    second contributes `balance × mult × usd × dt_days`. Run resets if balance dips
    below min_bal.

    Solstice quest description: "Daily accrual of rewards in relation to the
    investment allocation" — so bonus scales with current balance, not a flat
    threshold-payment.
    """
    if min_bal <= 0 or qualify_days <= 0 or not timeline: return 0.0
    qualify_sec = qualify_days * 86400
    flares = 0.0
    run_start = None
    is_callable = callable(usd_per_token)
    def _usd(t0, t1):
        if is_callable: return usd_per_token((t0 + t1) // 2)
        return usd_per_token
    # Build a list of (ts0, bal, ts1) segments, where the LAST one extends to end_ts
    segments = []
    for i in range(len(timeline) - 1):
        ts0, bal = timeline[i]
        ts1, _ = timeline[i + 1]
        if ts1 > end_ts: ts1 = end_ts
        if ts1 > ts0: segments.append((ts0, bal, ts1))
    # Tail
    last_t, last_b = timeline[-1]
    if last_t < end_ts: segments.append((last_t, last_b, end_ts))

    for ts0, bal, ts1 in segments:
        if bal >= min_bal:
            if run_start is None: run_start = ts0
            qualify_ts = run_start + qualify_sec
            earn_start = max(ts0, qualify_ts)
            if earn_start < ts1:
                dt_days = (ts1 - earn_start) / 86400.0
                flares += bal * _usd(earn_start, ts1) * mult * dt_days
        else:
            run_start = None
    return flares


# Back-compat alias for any external callers
_integrate_min_balance_bonus = _integrate_qualified_bonus


class HoldUSXExtractor(QuestExtractor):
    QUEST_CODE = ("S2_HOLD_USX_DAILY", "S2_HOLD_USX_1MO", "S2_HOLD_USX_3MO")
    MULTIPLIER = {"S2_HOLD_USX_DAILY": 10, "S2_HOLD_USX_1MO": 6, "S2_HOLD_USX_3MO": 15}
    SHARED_CACHE_KEY = "S2_HOLD_USX"

    def extract(self, wallet: str) -> dict:
        prior = load_quest_cache(self.cache_key(), wallet)
        prior_atas = ((prior or {}).get("raw") or {}).get("atas") or []
        return _build_timeline(wallet, USX_MINT, int(time.time()), prior_atas)

    def transform(self, raw: dict, now_ts: int) -> dict:
        timeline = raw.get("timeline") or []
        end_ts = min(now_ts, S2_END_TS)
        usd_per = 1.0  # USX
        out = {}
        out["S2_HOLD_USX_DAILY"] = _integrate_twab(timeline, 10, usd_per, end_ts)
        out["S2_HOLD_USX_1MO"]   = _integrate_qualified_bonus(
            timeline, min_bal=100.0, qualify_days=30, mult=6, usd_per_token=usd_per, end_ts=end_ts)
        out["S2_HOLD_USX_3MO"]   = _integrate_qualified_bonus(
            timeline, min_bal=100.0, qualify_days=90, mult=15, usd_per_token=usd_per, end_ts=end_ts)
        return out
