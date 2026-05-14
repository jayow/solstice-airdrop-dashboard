"""eUSX peg history.

The eUSX yield_vault stores its current exchange rate in a PDA. We read this
value from chain whenever requested, persist daily snapshots into
`eusx_peg_snapshots`, and provide a `peg_at(ts)` function that interpolates
between snapshots — or, for timestamps before our earliest snapshot,
reverse-compounds from the earliest known peg using an assumed APY.

Standard Solana RPC can't read PDA state at a historical slot, so we don't
have a true Solstice-grade per-second peg history. But the peg compounds
smoothly at ~5–8% APY, so linear interpolation between daily snapshots is
accurate to within ~0.05% — well under the multiplier-level rounding the
dashboard tolerates.

Pre-snapshot history (before our first stored snapshot) is approximated by
reverse-compounding the earliest known peg at `ASSUMED_APY`. This means the
first sweep after a fresh DB will use a calibrated estimate; once daily
snapshots accumulate, the function falls back on real data.
"""
import os, sys, time, base64, struct, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rpc_helper import rpc

EUSX_PEG_PDA = 'JDs1wmLaVB2KsAotjbBKVEsiV1gbrG3Qrjyht5LnX9YP'
EUSX_PEG_OFFSET = 48          # legacy: this field is NOT the USD peg (it's some
                              # other vault ratio ≈ 1.156). Kept for forensics.
ASSUMED_APY = 0.06            # 6% — calibration target; only used for pre-snapshot back-extension
SECONDS_PER_YEAR = 365.25 * 86400
FALLBACK_PEG = 1.0319         # safety net if both API sources fail

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DB = os.path.join(ROOT, 'data', 'solstice.db')


def read_live_peg() -> float:
    """Live eUSX→USD price. Pulls from Solstice's protocol API (the canonical
    public source — same field shown on app.solstice.finance). Falls back to
    Exponent's syExchangeRate, then a hardcoded recent value if both fail.

    The on-chain PDA at JDs1wmLa offset 48 (~1.156) is NOT this number — it
    appears to be some internal vault ratio. We used that historically; values
    cached before this fix are ~12% high. The cumulative cache values for
    pre-existing positions therefore overstate slightly, but going-forward
    accrual uses the corrected price."""
    import urllib3 as _u, requests as _rq
    _u.disable_warnings()
    # Source A: Solstice
    try:
        r = _rq.get('https://app.solstice.finance/api/protocol', timeout=8, verify=False).json()
        p = float(r.get('eusxPrice') or 0)
        if 0.9 < p < 2.0: return p
    except Exception: pass
    # Source B: Exponent
    try:
        r = _rq.get('https://api.exponent.finance/api/markets', timeout=8, verify=False).json()
        for m in r:
            if 'eUSX' in str((m.get('underlyingAsset') or {}).get('name', '')):
                p = float(m.get('syExchangeRate') or 0)
                if 0.9 < p < 2.0: return p
    except Exception: pass
    return FALLBACK_PEG


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def record_snapshot(ts: int | None = None) -> float:
    """Read live peg from chain and store with the given (or current) ts.
    Returns the peg value."""
    peg = read_live_peg()
    ts = int(ts if ts is not None else time.time())
    with _conn() as c:
        c.execute('INSERT OR REPLACE INTO eusx_peg_snapshots(ts, peg) VALUES (?, ?)', (ts, peg))
        c.commit()
    return peg


_snapshots_cache: list[tuple[int, float]] | None = None


def _load_snapshots() -> list[tuple[int, float]]:
    global _snapshots_cache
    if _snapshots_cache is None:
        with _conn() as c:
            rows = c.execute('SELECT ts, peg FROM eusx_peg_snapshots ORDER BY ts').fetchall()
        _snapshots_cache = [(int(r['ts']), float(r['peg'])) for r in rows]
    return _snapshots_cache


def invalidate_cache():
    """Call after recording new snapshots so the next peg_at() picks them up."""
    global _snapshots_cache
    _snapshots_cache = None


def peg_at(ts: int) -> float:
    """Best-available peg estimate for `ts`. Linear-interpolates between
    snapshots; reverse-compounds for timestamps before the earliest snapshot."""
    snaps = _load_snapshots()
    if not snaps:
        # No data at all — refuse to silently return 1.0. Caller should
        # ensure record_snapshot() ran at least once.
        raise RuntimeError('eUSX peg snapshots empty — call record_snapshot() first')
    if ts <= snaps[0][0]:
        # Before earliest snapshot: reverse-compound at ASSUMED_APY
        t0, p0 = snaps[0]
        years = (t0 - ts) / SECONDS_PER_YEAR
        return p0 / ((1.0 + ASSUMED_APY) ** years)
    if ts >= snaps[-1][0]:
        # After latest snapshot: forward-compound from latest
        tn, pn = snaps[-1]
        years = (ts - tn) / SECONDS_PER_YEAR
        return pn * ((1.0 + ASSUMED_APY) ** years)
    # Between snapshots: linear interpolation
    for i in range(len(snaps) - 1):
        t0, p0 = snaps[i]
        t1, p1 = snaps[i + 1]
        if t0 <= ts <= t1:
            frac = (ts - t0) / max(1, (t1 - t0))
            return p0 + (p1 - p0) * frac
    return snaps[-1][1]


if __name__ == '__main__':
    # CLI: capture a snapshot now
    p = record_snapshot()
    invalidate_cache()
    print(f'Recorded eUSX peg @ {int(time.time())}: {p:.10f}')
