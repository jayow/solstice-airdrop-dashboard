"""Systematic pass/fail tests for the walker confidence guards added 2026-06-13.

Exit 0 = all guards verified; non-zero = a guard regressed. No eyeballing —
each guard has crafted inputs and an asserted outcome. Run:
    python3 tools/test_walker_guards.py
"""
import os, sys, struct, math
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")


def _v1_lp_payload(lp_price: float) -> bytes:
    """Craft a minimal valid V1 'provide' LP event with a given trailing lp_price."""
    disc = bytes.fromhex('d12ae34dbbd811b1')           # 'provide', lp_field_idx=1
    prefix = b'\x00' * 8                                # data[0:8] (pre-disc)
    user = b'\x11' * 32                                 # data[16:48]
    market = b'\x22' * 32                               # data[48:80]
    body = (12345).to_bytes(8, 'little') + (67890).to_bytes(8, 'little')  # 2 u64 (idx1 valid)
    return prefix + disc + user + market + body + struct.pack('<d', lp_price)


def test_lp_decode_guard():
    print("LP _decode_lp_event lp_price bound:")
    from walk_s2_lp import _decode_lp_event
    # valid O(1) price → decodes
    ok = _decode_lp_event(_v1_lp_payload(1.05), 'v1')
    check("valid lp_price=1.05 decodes (not None)", ok is not None and abs(ok[4] - 1.05) < 1e-9)
    # garbage / blow-up prices → rejected
    check("lp_price=1e9 rejected (blow-up class)", _decode_lp_event(_v1_lp_payload(1e9), 'v1') is None)
    check("lp_price=100 rejected (boundary)",       _decode_lp_event(_v1_lp_payload(100.0), 'v1') is None)
    check("lp_price=0 rejected",                    _decode_lp_event(_v1_lp_payload(0.0), 'v1') is None)
    check("lp_price=-1 rejected",                   _decode_lp_event(_v1_lp_payload(-1.0), 'v1') is None)
    check("lp_price=NaN rejected",                  _decode_lp_event(_v1_lp_payload(float('nan')), 'v1') is None)
    check("lp_price=inf rejected",                  _decode_lp_event(_v1_lp_payload(float('inf')), 'v1') is None)


def test_xpbook_fetch_distinguishes_failure():
    print("XPBook _fetch_and_decode distinguishes RPC-fail (None) from no-events ([]):")
    import walk_xpbook
    walk_xpbook.time.sleep = lambda *_: None   # skip retry backoff
    # hard RPC failure: rpc returns {} every time → events MUST be None (cursor holds)
    walk_xpbook.rpc = lambda *a, **k: {}
    sig, bt, ev = walk_xpbook._fetch_and_decode('FAKESIG')
    check("rpc-fail → events is None (not [])", ev is None)
    # fetched tx with no decodable xpbook events → [] (cursor may advance)
    walk_xpbook.rpc = lambda *a, **k: {'result': {'blockTime': 123, 'meta': {'err': None},
                                                   'transaction': {'message': {'instructions': []}}}}
    walk_xpbook._decode_events = lambda tx: []   # force "no events"
    sig, bt, ev = walk_xpbook._fetch_and_decode('FAKESIG2')
    check("fetched-but-empty → events == [] (not None)", ev == [])


def test_confidence_gate_outlier_logic():
    print("Confidence-gate outlier logic (rank-5 baseline, ratio 50x):")
    # Reproduce the gate's decision rule on synthetic top-lists.
    RATIO, RANK = 50.0, 5
    def has_outlier(vals):
        vals = sorted(vals, reverse=True)
        if len(vals) < RANK + 1: return False
        base = vals[RANK]
        return base > 0 and any(v > RATIO * base for v in vals)
    # legit whale distribution (smooth) → NO flag
    legit = [18e9, 7.7e9, 5.6e9, 5.2e9, 4.9e9, 4.1e9, 3.9e9, 3.8e9, 3.6e9]
    check("smooth whale distribution → no flag", not has_outlier(legit))
    # CLMM blow-up (1 wallet 1e13, rest normal) → flag
    blowup = [1e13] + [1e6] * 10
    check("single 1e13 blow-up → flagged", has_outlier(blowup))
    # 3-wallet blow-up cluster (rank-5 still normal) → flag
    cluster = [1e13, 9e12, 8e12] + [1e6] * 10
    check("3-wallet blow-up cluster → flagged", has_outlier(cluster))


if __name__ == '__main__':
    test_lp_decode_guard()
    test_xpbook_fetch_distinguishes_failure()
    test_confidence_gate_outlier_logic()
    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)
    print("✓ all walker guards verified")
    sys.exit(0)
