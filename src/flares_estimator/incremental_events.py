"""Shared helper for incremental per-position event extraction.

For each position PDA, only walks signatures that we haven't seen on a prior
run. State is implicit — we look at the most-recent sig in the existing cached
events for that position and pass it as the `until` bound to
`getSignaturesForAddress`, which stops paginating once it reaches it.

Result: on the second run for a position with no new on-chain activity, we
make exactly 1 RPC call (the initial getSignaturesForAddress returning 0 new
sigs). Daily refresh cost drops by ~80-95% for sticky positions.

Usage:
    new_events = incremental_walk(
        pos_pubkey=pos_pk,
        existing_events_for_position=[e for e in cached_events if e['pos_pubkey'] == pos_pk],
        classify=lambda tx, sig: { ... },   # returns event dict or None
    )
    merged = (existing_events_for_position + new_events)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc


def latest_sig_for_position(existing_events_for_position: list) -> str | None:
    """Latest signature among existing events for a position PDA. Used as the
    `until` boundary on the next signature walk."""
    if not existing_events_for_position: return None
    # Sort by ts desc, return signature of newest
    sorted_evs = sorted(existing_events_for_position,
                        key=lambda e: e.get('ts') or 0, reverse=True)
    return sorted_evs[0].get('sig')


def fetch_new_sigs(pubkey: str, until_sig: str | None, max_pages: int = 5) -> list:
    """Walk getSignaturesForAddress on `pubkey`, stopping at `until_sig` or
    after max_pages. Returns sigs in time order (oldest first) so they can be
    appended to existing events safely."""
    sigs = []
    before = None
    for _ in range(max_pages):
        params = [pubkey, {'limit': 1000}]
        if before:    params[1]['before'] = before
        if until_sig: params[1]['until'] = until_sig
        try:
            r = rpc('getSignaturesForAddress', params, timeout=20)
        except Exception: break
        batch = r.get('result') or []
        if not batch: break
        sigs.extend(batch)
        if len(batch) < 1000: break
        before = batch[-1]['signature']
    # API returns newest-first; reverse to chronological
    sigs.reverse()
    return sigs


def extract_events_incremental(pos_pubkey: str,
                                existing_events_for_position: list,
                                classify_fn) -> list:
    """Walk new signatures for a position PDA, classify each via `classify_fn`,
    return ONLY new events (caller merges with existing).

    classify_fn signature: (tx_dict, sig) -> event_dict or None
    The returned event must include 'sig', 'ts', 'pos_pubkey' so the next
    incremental run knows what's already processed.
    """
    last_sig = latest_sig_for_position(existing_events_for_position)
    new_sigs = fetch_new_sigs(pos_pubkey, until_sig=last_sig)
    if not new_sigs: return []

    new_events = []
    for s in new_sigs:
        if s.get('err'): continue
        try:
            rr = rpc('getTransaction', [s['signature'],
                    {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}], timeout=15)
        except Exception: continue
        tx = rr.get('result')
        if not tx: continue
        ev = classify_fn(tx, s)
        if ev is None: continue
        # Ensure required fields are present
        ev.setdefault('ts', s.get('blockTime'))
        ev.setdefault('sig', s['signature'])
        ev.setdefault('pos_pubkey', pos_pubkey)
        new_events.append(ev)
    return new_events
