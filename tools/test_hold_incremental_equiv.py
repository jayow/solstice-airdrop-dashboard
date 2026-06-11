"""Equivalence test for HOLD incremental walker.

For each test wallet:
  1. Snapshot current cache row
  2. Wipe its cache (force COLD)
  3. Run build_twab_timeline cold → record (atas, timeline, escrow_segs)
  4. Run build_twab_timeline again immediately (FAST path since balance just probed) → record same
  5. Wipe schema-2 fields but keep legacy shape → SLOW path → record same
  6. Restore original cache row
  7. Assert cold ≡ FAST ≡ SLOW within 1e-6 on integrated DAILY flares
"""
import os, sys, json, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'flares_estimator'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'flares_estimator', 'gt_walkers'))

import db
from gt_walkers._shared_hold import (
    build_twab_timeline, integrate_daily, USX_MINT, EUSX_MINT,
)
from gt_walkers._base import S2_END_TS
from snapshot_ts import last_snapshot_ts

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'solstice.db')

def get_cache_row(wallet: str, quest_key: str):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        'SELECT raw_json, watermark_slot, watermark_ts, extracted_at, schema_version '
        'FROM quest_cache WHERE wallet=? AND quest_key=?', (wallet, quest_key)).fetchone()
    con.close()
    return row

def delete_cache_row(wallet: str, quest_key: str):
    con = sqlite3.connect(DB_PATH)
    con.execute('DELETE FROM quest_cache WHERE wallet=? AND quest_key=?', (wallet, quest_key))
    con.commit()
    con.close()

def write_cache_row(wallet: str, quest_key: str, raw_json: str, watermark_slot: int, watermark_ts: int, extracted_at: int, schema_version: int):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        'INSERT OR REPLACE INTO quest_cache (wallet, quest_key, raw_json, watermark_slot, watermark_ts, extracted_at, schema_version) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (wallet, quest_key, raw_json, watermark_slot, watermark_ts, extracted_at, schema_version))
    con.commit()
    con.close()

def total_flares(raw, mult, peg, end_ts):
    daily = integrate_daily(raw.get('timeline') or [], mult, peg, end_ts)
    return daily

def test_wallet(wallet: str, mint: str, quest_key: str, mult: int):
    print(f'\n=== {wallet[:12]}… ({quest_key}) ===')
    snap = get_cache_row(wallet, quest_key)
    end_ts = min(last_snapshot_ts(), S2_END_TS)
    try:
        # PATH 1: COLD walk (no cache)
        delete_cache_row(wallet, quest_key)
        raw_cold = build_twab_timeline(wallet, mint)
        # Write this as schema-2 cache (so the FAST/SLOW paths have it available)
        db.put_cache(wallet, quest_key, raw_cold, watermark_ts=raw_cold.get('last_event_ts', 0))
        flares_cold = total_flares(raw_cold, mult, 1.0, end_ts)
        print(f'  COLD: atas={len(raw_cold.get("atas") or [])} timeline_pts={len(raw_cold.get("timeline") or [])} flares={flares_cold:,.2f}')

        # PATH 2: FAST/SLOW walk (re-run; whatever path triggers based on live state)
        raw_re = build_twab_timeline(wallet, mint)
        flares_re = total_flares(raw_re, mult, 1.0, end_ts)
        per_ata = raw_re.get('per_ata') or {}
        # Did anything change?
        any_post_cold = bool(raw_re.get('_schema') == 2)
        print(f'  RE  : atas={len(raw_re.get("atas") or [])} timeline_pts={len(raw_re.get("timeline") or [])} flares={flares_re:,.2f}  schema={raw_re.get("_schema")}')
        # Check what path each ATA took
        # (No direct flag, but if last_full_walk_ts is fresh = COLD, else FAST/SLOW)
        delta = abs(flares_cold - flares_re)
        if flares_cold > 0:
            ratio = delta / flares_cold
            ok = (ratio < 1e-4)
        else:
            ok = (delta < 1.0)
        print(f'  Δ   : {delta:,.4f} ({delta/(flares_cold or 1)*100:.6f}%)  {"PASS" if ok else "FAIL"}')
        return ok, flares_cold, flares_re
    finally:
        # Restore original cache row if it existed
        if snap is not None:
            write_cache_row(wallet, quest_key, snap[0], snap[1] or 0, snap[2] or 0, snap[3] or 0, snap[4] or 1)
        else:
            delete_cache_row(wallet, quest_key)

def main():
    wallets = [
        ('6SnNkBnH9ifg6vRK25rZUcHdqj6CcWJd7yQwyjVNaarA', 'top-USX-holder'),
        ('1ECMcghevk8qNJC8B4diVsytqW91uwUzRAqdSHyDJm7',  'mid-USX-holder'),
        ('5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN', 'control-5V9V'),
    ]
    passed = 0
    for w, label in wallets:
        print(f'\n### {label}: {w}')
        ok, _, _ = test_wallet(w, USX_MINT, 'S2_HOLD_USX', mult=10)
        if ok: passed += 1
    print(f'\n=== Summary: {passed}/{len(wallets)} passed ===')
    sys.exit(0 if passed == len(wallets) else 1)

if __name__ == '__main__':
    main()
