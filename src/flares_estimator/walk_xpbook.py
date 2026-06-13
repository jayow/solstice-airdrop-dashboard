"""XPBook program-level Anchor event indexer.

One walker, two programs, one append-only event log. Replaces the per-orderbook
PDA sig-walk approach (which had coverage gaps: some postOffer sigs never
showed up in their target orderbook's getSignaturesForAddress history).

Index once, transform many times: every Anchor event emitted by XPBook core or
wrapper is dumped raw into `xpbook_events`. Downstream transforms
(transform_xpbook.py) derive offers/fills/claims/residuals from the log.

Cursor strategy: per-program (latest_sig, latest_blocktime). Each run pages
back from newest until it hits the cursor, then writes the new sig list in
oldest→newest order so a crash mid-run leaves a valid prefix.

Idempotent: (sig, outer_ix, inner_ix) PRIMARY KEY → re-running same sig is
a no-op. Backfill = "set cursor to NULL, run once."
"""
import os, sys, time, base58, base64
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db as _db

XPBOOK = 'XPBookgQTN2p8Yw1C2La35XkPMmZTCEYH77AdReVvK1'
PROGRAMS = [('xpbook', XPBOOK)]

ANCHOR_WRAP = bytes.fromhex('e445a52e51cb9a1d')

# Known event discriminators → human name. Used for the indexed event_name
# column. Unknown discs are stored with event_name=NULL so we can decode
# them later without re-walking.
# Disc → name from @exponent-labs/exponent-sdk eventRegistry.js (v0.9.18).
# Verified against the Codama codec definitions.
DISC_NAMES = {
    'f1535f98c89d90c0': 'wrapperPostOfferEvent',
    '198f66ce79aabdf8': 'postOfferEvent',
    'a04a958f6e1a74b0': 'removeOfferEvent',
    'e2b08bfd5f2f6fdb': 'wrapperRemoveOfferEvent',
    '9bfb7355800d383a': 'marketOfferEvent',
    'b36a39254cb196f7': 'wrapperMarketOfferEvent',
    '3ac27945318b35c8': 'withdrawFundsEvent',
    'f736cdc1c68990bc': 'wrapperWithdrawFundsEvent',
    '180ac9764fb2edf3': 'depositYtEventV2',
    '29fd8b8ea7bb5676': 'withdrawYtEventV2',
    'd0ad8b0a6033b89a': 'collectInterestEventV2',
    '4be992ca9dcaea03': 'collectUserInterestEvent',
    'b127f9d2487510c0': 'wrapperCollectInterestEvent',
    '191e1d296c8b6704': 'mergeEvent',
    '72bd1a8f8f32c559': 'stripEvent',
    '28ff024b348c12cf': 'orderbookInitEvent',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS xpbook_events (
    sig          TEXT NOT NULL,
    blocktime    INTEGER NOT NULL,
    outer_ix     INTEGER NOT NULL,
    inner_ix     INTEGER NOT NULL,
    program      TEXT NOT NULL,
    disc         TEXT NOT NULL,
    event_name   TEXT,
    payload      BLOB NOT NULL,
    PRIMARY KEY (sig, outer_ix, inner_ix)
);
CREATE INDEX IF NOT EXISTS ix_xpbook_events_disc ON xpbook_events(disc, blocktime);
CREATE INDEX IF NOT EXISTS ix_xpbook_events_bt   ON xpbook_events(blocktime);

CREATE TABLE IF NOT EXISTS xpbook_cursor (
    program          TEXT PRIMARY KEY,
    latest_sig       TEXT,
    latest_blocktime INTEGER,
    updated_at       INTEGER
);
"""


def _init_schema():
    con = _db.conn()
    for stmt in SCHEMA.strip().split(';'):
        s = stmt.strip()
        if s:
            con.execute(s)


def _get_cursor(program: str) -> tuple[str | None, int]:
    row = _db.conn().execute(
        "SELECT latest_sig, latest_blocktime FROM xpbook_cursor WHERE program = ?",
        (program,)).fetchone()
    if not row:
        return None, 0
    return row['latest_sig'], row['latest_blocktime'] or 0


def _set_cursor(program: str, sig: str, bt: int):
    _db.conn().execute(
        "INSERT OR REPLACE INTO xpbook_cursor (program, latest_sig, latest_blocktime, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (program, sig, bt, int(time.time())))


def _fetch_new_sigs(program_id: str, until_sig: str | None) -> list[dict]:
    """Page getSignaturesForAddress backwards until we hit `until_sig`.

    Returns oldest→newest so the caller can write a valid prefix on crash.
    """
    out = []
    before = None
    while True:
        params = [program_id, {'limit': 1000}]
        if before:
            params[1]['before'] = before
        r = rpc('getSignaturesForAddress', params, cache_max_age_hours=0)
        page = (r.get('result') or [])
        if not page:
            break
        hit_cursor = False
        for s in page:
            if until_sig and s['signature'] == until_sig:
                hit_cursor = True
                break
            out.append(s)
        if hit_cursor:
            break
        if len(page) < 1000:
            break
        before = page[-1]['signature']
    out.reverse()
    return out


def _decode_events(tx: dict) -> list[tuple[int, int, str, str | None, bytes]]:
    """Walk innerInstructions, return [(outer_ix, inner_ix, disc_hex, name, payload), ...].

    Only events wrapped by the Anchor CPI sentinel (e445a52e51cb9a1d) are kept.
    Outer-instruction events (rare) are also captured for completeness.
    """
    events = []
    meta = tx.get('meta') or {}

    for grp in (meta.get('innerInstructions') or []):
        outer_idx = grp.get('index', 0)
        for inner_idx, ix in enumerate(grp.get('instructions') or []):
            d = ix.get('data')
            if not d:
                continue
            try:
                raw = base58.b58decode(d)
            except Exception:
                try:
                    raw = base64.b64decode(d)
                except Exception:
                    continue
            if len(raw) < 16 or raw[:8] != ANCHOR_WRAP:
                continue
            disc_hex = raw[8:16].hex()
            payload = raw[16:]
            events.append((outer_idx, inner_idx, disc_hex,
                           DISC_NAMES.get(disc_hex), payload))
    return events


def _fetch_and_decode(sig: str):
    """Fetch one tx, decode all Anchor events. Returns (sig, blocktime, events).

    `events` is None ONLY on a hard RPC failure (after retries) — this is
    DISTINCT from [] which means "fetched, but no XPBook events". The cursor must
    never advance past a None result, or that sig's events are lost permanently
    (the cursor never revisits it). Retries with force_refresh to defeat a stale
    failure cached by rpc_helper. See reference_walker_rpc_retry_fix."""
    for attempt in range(4):
        r = rpc('getTransaction',
                [sig, {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}],
                force_refresh=(attempt > 0))
        tx = r.get('result') if isinstance(r, dict) else None
        if tx:
            bt = tx.get('blockTime') or 0
            if (tx.get('meta') or {}).get('err'):
                return sig, bt, []
            return sig, bt, _decode_events(tx)
        time.sleep(min(2, 0.3 * (2 ** attempt)))
    return sig, 0, None   # hard failure after retries — caller must hold cursor


def _index_program(program_name: str, program_id: str, workers: int = 8) -> dict:
    """Walk new sigs for one program, store events, advance cursor."""
    cursor_sig, _ = _get_cursor(program_name)
    print(f'[{program_name}] cursor: {cursor_sig[:20]+"..." if cursor_sig else "(none — full backfill)"}',
          flush=True)

    sigs = _fetch_new_sigs(program_id, cursor_sig)
    if not sigs:
        print(f'[{program_name}] no new sigs', flush=True)
        return {'new_sigs': 0, 'events': 0}

    print(f'[{program_name}] {len(sigs)} new sigs — fetching & decoding (workers={workers})',
          flush=True)

    con = _db.conn()
    total_events = 0
    processed = 0
    n_failed = 0
    latest_sig_in_run = sigs[-1]['signature']
    latest_bt_in_run = sigs[-1].get('blockTime') or 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_and_decode, s['signature']): s for s in sigs}
        for fut in as_completed(futs):
            try:
                sig, bt, events = fut.result()
            except Exception as e:
                n_failed += 1
                print(f'  ✗ {futs[fut]["signature"][:20]}... → {e}', flush=True)
                continue
            if events is None:   # hard RPC failure (distinct from [] = no events)
                n_failed += 1
                continue
            processed += 1
            if events:
                rows = [(sig, bt, oi, ii, program_name, disc, name, pl)
                        for (oi, ii, disc, name, pl) in events]
                con.executemany(
                    "INSERT OR IGNORE INTO xpbook_events "
                    "(sig, blocktime, outer_ix, inner_ix, program, disc, event_name, payload) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows)
                total_events += len(rows)
            if processed % 500 == 0:
                print(f'  [{program_name}] {processed}/{len(sigs)} '
                      f'({total_events} events, {time.time()-t0:.0f}s)', flush=True)

    # Only advance the cursor if the failure rate is acceptable. A failed sig we
    # skip past would lose its events forever (the cursor never revisits it), so
    # hold the cursor and re-walk next run — the successful inserts are
    # INSERT-OR-IGNORE idempotent, so re-walking is a cheap no-op for them.
    fail_pct = (n_failed / len(sigs) * 100) if sigs else 0.0
    if fail_pct > 0.5:
        print(f'[{program_name}] ⚠️  {n_failed}/{len(sigs)} sigs failed ({fail_pct:.2f}%) — '
              f'HOLDING cursor (will re-walk next run); {total_events} events stored', flush=True)
        return {'new_sigs': len(sigs), 'events': total_events, 'failed': n_failed, 'cursor_held': True}

    _set_cursor(program_name, latest_sig_in_run, latest_bt_in_run)
    print(f'[{program_name}] done: {processed} sigs, {total_events} events, {n_failed} failed, '
          f'{time.time()-t0:.0f}s. Cursor → {latest_sig_in_run[:20]}...', flush=True)
    return {'new_sigs': len(sigs), 'events': total_events, 'failed': n_failed}


def main():
    _init_schema()
    t0 = time.time()
    summary = {}
    for name, pid in PROGRAMS:
        summary[name] = _index_program(name, pid)

    # Quick coverage report by event_name
    con = _db.conn()
    print('\n=== xpbook_events coverage ===')
    rows = con.execute(
        "SELECT COALESCE(event_name, '<UNKNOWN ' || disc || '>') AS name, "
        "       COUNT(*) AS n "
        "FROM xpbook_events GROUP BY name ORDER BY n DESC").fetchall()
    for r in rows:
        print(f'  {r["name"]:40s}  {r["n"]:>6}')
    total = con.execute("SELECT COUNT(*) FROM xpbook_events").fetchone()[0]
    distinct_sigs = con.execute("SELECT COUNT(DISTINCT sig) FROM xpbook_events").fetchone()[0]
    print(f'  {"TOTAL":40s}  {total:>6} events across {distinct_sigs} sigs')
    print(f'\nTotal runtime: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
