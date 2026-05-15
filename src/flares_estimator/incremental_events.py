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


def fetch_new_sigs(pubkey: str, until_sig: str | None, max_pages: int = 5,
                   walker_name: str = '?') -> list:
    """Walk getSignaturesForAddress on `pubkey`, stopping at `until_sig` or
    after max_pages. Returns sigs in time order (oldest first) so they can be
    appended to existing events safely.

    If we hit `max_pages` AND the last page came back full (1000 sigs), the
    walk was truncated — we don't know what we missed. Log to walker_saturation
    so audit can flag it.
    """
    sigs = []
    before = None
    pages_used = 0
    last_batch_full = False
    for _ in range(max_pages):
        pages_used += 1
        params = [pubkey, {'limit': 1000}]
        if before:    params[1]['before'] = before
        if until_sig: params[1]['until'] = until_sig
        try:
            r = rpc('getSignaturesForAddress', params, timeout=20)
        except Exception: break
        batch = r.get('result') or []
        if not batch:
            last_batch_full = False
            break
        sigs.extend(batch)
        last_batch_full = (len(batch) == 1000)
        if not last_batch_full: break
        before = batch[-1]['signature']
    if pages_used == max_pages and last_batch_full:
        try:
            import time as _t, db as _db
            _db.init()
            _db.conn().execute(
                'INSERT INTO walker_saturation (ts, pubkey, walker, sigs_seen, max_pages) '
                'VALUES (?, ?, ?, ?, ?)',
                (int(_t.time()), pubkey, walker_name, len(sigs), max_pages)
            )
        except Exception: pass
    sigs.reverse()
    return sigs


def extract_events_incremental(pos_pubkey: str,
                                existing_events_for_position: list,
                                classify_fn,
                                walker_name: str = '?') -> list:
    """Walk new signatures for a position PDA, classify each via `classify_fn`,
    return ONLY new events (caller merges with existing).

    classify_fn signature: (tx_dict, sig) -> event_dict or None
    The returned event must include 'sig', 'ts', 'pos_pubkey' so the next
    incremental run knows what's already processed.
    """
    last_sig = latest_sig_for_position(existing_events_for_position)
    new_sigs = fetch_new_sigs(pos_pubkey, until_sig=last_sig, walker_name=walker_name)
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
