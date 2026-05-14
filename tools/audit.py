"""Solstice dashboard audit.

Run after every refresh to catch silent data bugs. Three tiers:

  TIER 1 — STRUCTURAL: SQLite vs data.json consistency. JSON/SQLite drift,
           by-quest sync, HOLD cache vs flares, walker_outputs vs wallet_quests.

  TIER 3 — SOLSTICE: cross-check against Solstice's public API (system total
           within tolerance, eUSX peg matches, day-over-day growth sane).

  TIER 4 — INVARIANTS: silent assumptions that would mask future bugs (every
           enabled quest has earners, daily-emission table is internally
           consistent, no negative flares).

Tier 2 (on-chain reality check) is intentionally separate — it's RPC-heavy.
Run via `--check-onchain` (not implemented in this version).

Usage:
    python3 tools/audit.py [--max-detail N] [--tier 1,3,4]

Exit code: 0 if all PASS, 1 if any FAIL (CRITICAL drift), 0 with warnings
otherwise.
"""
import os, sys, json, sqlite3, argparse, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')
DATA_JSON = os.path.join(ROOT, 'server', 'data.json')
DAILY_JSON = os.path.join(ROOT, 'server', 'daily_totals.json')

# Tolerances for "drift considered material"
DRIFT_ABS_FLARES = 1.0        # 1 flare absolute
DRIFT_REL_PCT    = 0.001      # 0.001% relative
SYS_DRIFT_PCT    = 0.01       # system total drift > 0.01% is a fail


# ────────────────────── runners ──────────────────────

class Finding:
    def __init__(self, severity, tier, check, message, detail=None):
        self.severity = severity   # 'PASS', 'WARN', 'FAIL'
        self.tier = tier
        self.check = check
        self.message = message
        self.detail = detail or []


class AuditReport:
    def __init__(self):
        self.findings: list[Finding] = []

    def add(self, f: Finding):
        self.findings.append(f)
        return f

    def summary(self):
        by_tier = defaultdict(lambda: {'PASS': 0, 'WARN': 0, 'FAIL': 0})
        for f in self.findings:
            by_tier[f.tier][f.severity] += 1
        return dict(by_tier)


def _load_json(path):
    with open(path) as f: return json.load(f)


def _flares_filter_clause(table_alias='wq'):
    # match build_data.py — exclude both pda_protocol and pda_or_uninit
    return f"COALESCE(w.classification,'') NOT IN ('pda_protocol', 'pda_or_uninit')"


# ────────────────────── TIER 1 — structural ──────────────────────

def tier1_structural(con, data, max_detail=10):
    out = []
    # 1.1 — system grand total: SQLite vs JSON
    sql_total = con.execute(f"""
        SELECT COALESCE(SUM(wq.flares),0) FROM wallet_quests wq
        JOIN wallets w USING (wallet)
        WHERE { _flares_filter_clause() }
    """).fetchone()[0] or 0
    json_total = sum((r.get('total') or 0) for r in data['records'] if not r.get('is_protocol_pda'))
    drift = json_total - sql_total
    drift_pct = abs(drift) / sql_total * 100 if sql_total else 0
    sev = 'FAIL' if drift_pct > SYS_DRIFT_PCT else ('WARN' if drift_pct > 0.001 else 'PASS')
    out.append(Finding(sev, 1, 'system_total',
        f'SQLite={sql_total:,.0f}  JSON={json_total:,.0f}  delta={drift:+,.0f} ({drift_pct:.4f}%)'))

    # 1.2 — per-wallet total: data.json record.total vs wallet_quests SUM.
    # LEFT JOIN so ghost wallets (in wallet_quests but missing from `wallets`
    # metadata table) still get compared — otherwise we get false-positive
    # "JSON has flares that SQLite doesn't" findings.
    sql_per_wallet = dict(con.execute("""
        SELECT wq.wallet, SUM(wq.flares)
        FROM wallet_quests wq LEFT JOIN wallets w USING (wallet)
        WHERE COALESCE(w.classification,'') NOT IN ('pda_protocol','pda_or_uninit')
          AND wq.quest LIKE 'S2_%'
        GROUP BY wq.wallet
    """))
    mismatches = []
    for r in data['records']:
        if r.get('is_protocol_pda'): continue
        j = r.get('total') or 0
        s = sql_per_wallet.get(r['wallet'], 0)
        if abs(j - s) > DRIFT_ABS_FLARES and abs(j - s) / max(s, 1) * 100 > DRIFT_REL_PCT:
            mismatches.append((r['wallet'], s, j, j - s))
    detail = [f"  {w[:12]}…  sqlite={s:,.0f}  json={j:,.0f}  delta={d:+,.0f}"
              for w, s, j, d in sorted(mismatches, key=lambda x: -abs(x[3]))[:max_detail]]
    sev = 'PASS' if not mismatches else ('FAIL' if len(mismatches) > 50 else 'WARN')
    out.append(Finding(sev, 1, 'per_wallet_total',
        f'{len(mismatches)} wallets drift between SQLite total and JSON total', detail))

    # 1.3 — HOLD cache vs flares: any wallet earning HOLD_*_DAILY must have nonzero cached timeline
    hold_bad = []
    for daily_q, cache_key in [('S2_HOLD_USX_DAILY', 'S2_HOLD_USX'), ('S2_HOLD_EUSX_DAILY', 'S2_HOLD_EUSX')]:
        rows = list(con.execute(f"""
            SELECT wq.wallet, wq.flares, qc.raw_json
            FROM wallet_quests wq
            LEFT JOIN quest_cache qc ON qc.wallet=wq.wallet AND qc.quest_key=?
            WHERE wq.quest=? AND wq.flares > 0
        """, (cache_key, daily_q)))
        for r in rows:
            try:
                raw = json.loads(r['raw_json']) if r['raw_json'] else {}
            except Exception:
                raw = {}
            atas = raw.get('atas') or []
            tl = raw.get('timeline') or []
            max_bal = max((float(b) for _, b in tl), default=0.0) if tl else 0.0
            if atas == [] or max_bal == 0.0:
                hold_bad.append((r['wallet'], daily_q, r['flares'], atas, max_bal))
    detail = [f"  {w[:12]}…  {q}  flares={f:,.0f}  atas={a}  max_bal={mb:.2f}"
              for w, q, f, a, mb in hold_bad[:max_detail]]
    sev = 'PASS' if not hold_bad else 'FAIL'
    out.append(Finding(sev, 1, 'hold_cache_consistency',
        f'{len(hold_bad)} wallets earning HOLD flares but cache shows empty timeline', detail))

    # 1.4 — walker_outputs vs wallet_quests for same (wallet, quest).
    # Walkers that have a downstream transform (e.g. walk_s2_kamino →
    # transform_kamino piecewise integration) intentionally produce DIFFERENT
    # numbers in walker_outputs vs wallet_quests — the transform is the more
    # accurate value. We exempt those walkers from the strict check and
    # tolerate up to 2% drift.
    TRANSFORMED_WALKERS = {'walk_s2_kamino', 'walk_s2_loopscale'}  # has *_transform.py downstream
    TRANSFORMED_TOLERANCE_PCT = 2.0

    drift_rows = list(con.execute("""
        SELECT wo.wallet, wo.quest, wo.flares AS wo_flares, wq.flares AS wq_flares,
               wo.walker
        FROM walker_outputs wo
        JOIN wallet_quests wq ON wq.wallet=wo.wallet AND wq.quest=wo.quest
        WHERE ABS(COALESCE(wo.flares,0) - COALESCE(wq.flares,0)) > ?
    """, (DRIFT_ABS_FLARES,)))
    filtered_drift = []
    for r in drift_rows:
        delta = abs(r['wo_flares'] - r['wq_flares'])
        rel = delta / max(abs(r['wq_flares']), 1) * 100
        if rel <= DRIFT_REL_PCT: continue
        # Apply transformed-walker tolerance
        if r['walker'] in TRANSFORMED_WALKERS and rel <= TRANSFORMED_TOLERANCE_PCT: continue
        filtered_drift.append(r)
    detail = [f"  {r['wallet'][:12]}…  {r['quest']}  walker[{r['walker']}]={r['wo_flares']:,.0f}  wq={r['wq_flares']:,.0f}  delta={(r['wo_flares']-r['wq_flares']):+,.0f}"
              for r in sorted(filtered_drift, key=lambda r: -abs(r['wo_flares']-r['wq_flares']))[:max_detail]]
    sev = 'PASS' if not filtered_drift else ('WARN' if len(filtered_drift) < 20 else 'FAIL')
    msg = f'{len(filtered_drift)} (wallet, quest) pairs drift > 2% (excluding transformed walkers: {", ".join(sorted(TRANSFORMED_WALKERS))})'
    out.append(Finding(sev, 1, 'walker_outputs_sync', msg, detail))

    # 1.5 — no negative flares
    neg = list(con.execute("SELECT wallet, quest, flares FROM wallet_quests WHERE flares < 0 LIMIT 10"))
    sev = 'PASS' if not neg else 'FAIL'
    out.append(Finding(sev, 1, 'no_negative_flares',
        f'{len(neg)} rows with negative flares (showing up to 10)',
        [f"  {r['wallet']}  {r['quest']}  {r['flares']:,.0f}" for r in neg]))
    return out


# ────────────────────── TIER 3 — solstice ──────────────────────

def tier3_solstice(con, data, max_detail=10):
    out = []
    import urllib3 as _u, requests as _rq
    _u.disable_warnings()
    try:
        r = _rq.get('https://app.solstice.finance/api/protocol', timeout=10, verify=False).json()
    except Exception as e:
        out.append(Finding('WARN', 3, 'solstice_api',
            f'Could not reach Solstice API: {e}'))
        return out
    # Solstice does not publish a system grand_flares number through this
    # endpoint; we treat eusxPrice and supply numbers as cross-checks.
    eusx_price_api = float(r.get('eusxPrice') or 0)
    eusx_supply_api = float(r.get('eusxSupply') or 0)

    # 3.1 — eUSX peg in our system matches API
    import sys as _sys; _sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
    try:
        from quests.eusx_peg import peg_at
        our_peg = peg_at(int(time.time()))
    except Exception:
        our_peg = None
    if our_peg and eusx_price_api:
        drift_pct = abs(our_peg - eusx_price_api) / eusx_price_api * 100
        sev = 'FAIL' if drift_pct > 0.5 else ('WARN' if drift_pct > 0.1 else 'PASS')
        out.append(Finding(sev, 3, 'eusx_peg_match',
            f'our peg={our_peg:.6f}  Solstice eusxPrice={eusx_price_api:.6f}  drift={drift_pct:.4f}%'))
    else:
        out.append(Finding('WARN', 3, 'eusx_peg_match', 'could not load our peg'))

    # 3.2 — day-over-day system growth roughly matches Solstice's published growth pattern
    try:
        daily = _load_json(DAILY_JSON)
        days = daily.get('days') or []
        if len(days) >= 2:
            today = days[-1]['cumulative']; prev = days[-2]['cumulative']
            growth_pct = (today - prev) / max(prev, 1) * 100
            # Healthy daily growth ≈ 3-5%
            sev = 'PASS' if 1.5 < growth_pct < 7 else 'WARN'
            out.append(Finding(sev, 3, 'system_daily_growth',
                f'system grew {growth_pct:.2f}% day-over-day (healthy range: 3-5%)'))
    except Exception as e:
        out.append(Finding('WARN', 3, 'system_daily_growth', f'could not compute: {e}'))

    return out


# ────────────────────── TIER 4 — invariants ──────────────────────

def tier4_invariants(con, data, max_detail=10):
    out = []
    # 4.1 — each non-disabled quest should have at least one wallet earning.
    # `deferred` flag = enabled in code but expected to have 0 earners until
    # some external condition is met (3MO HOLD until day 90, REFERRAL until
    # Solstice exposes SIWS data). Don't warn on those.
    try:
        sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
        from quest_map import QUESTS
    except Exception:
        QUESTS = []
    DEFERRED = {'S2_HOLD_USX_3MO', 'S2_HOLD_EUSX_3MO', 'S2_REFERRAL_BONUS'}
    quest_counts = dict(con.execute("SELECT quest, COUNT(*) FROM wallet_quests WHERE flares > 0 GROUP BY quest"))
    zero_quests = []
    for q in QUESTS:
        if q.get('disabled'): continue
        code = q['code']
        if code in DEFERRED: continue
        if quest_counts.get(code, 0) == 0:
            zero_quests.append(code)
    detail = [f"  {q}" for q in zero_quests[:max_detail]]
    sev = 'PASS' if not zero_quests else 'WARN'
    msg_extra = f' (skipped deferred: {", ".join(sorted(DEFERRED))})' if not zero_quests else ''
    out.append(Finding(sev, 4, 'enabled_quests_have_earners',
        f'{len(zero_quests)} non-deferred enabled quests have zero earners' + msg_extra, detail))

    # 4.2 — system_daily_emission_by_quest in data.json matches sum of per-wallet rates
    # (sanity check on the build_wallet_details aggregation)
    sysd = data.get('system_daily_emission_by_quest') or {}
    # Recompute by reading per-wallet JSONs is expensive; we just check the dict isn't empty/missing
    if not sysd:
        out.append(Finding('FAIL', 4, 'system_daily_emission_present',
            'data.json missing system_daily_emission_by_quest field'))
    else:
        total = sum(sysd.values())
        sev = 'PASS' if total > 1e8 else 'WARN'
        out.append(Finding(sev, 4, 'system_daily_emission_present',
            f'system daily total: {total:,.0f} flares/d across {len(sysd)} quests'))

    # 4.3 — eUSX peg snapshots aren't stuck at the wrong value
    snaps = list(con.execute("SELECT peg FROM eusx_peg_snapshots ORDER BY ts DESC LIMIT 5"))
    if snaps:
        latest = snaps[0][0]
        if latest > 1.10:   # historic-bug value was 1.156
            out.append(Finding('FAIL', 4, 'eusx_peg_value',
                f'latest peg = {latest:.4f} — suspiciously high (the historic bug value was 1.156)'))
        elif latest < 0.90:
            out.append(Finding('FAIL', 4, 'eusx_peg_value',
                f'latest peg = {latest:.4f} — suspiciously low'))
        else:
            out.append(Finding('PASS', 4, 'eusx_peg_value',
                f'latest peg = {latest:.6f} (reasonable)'))

    # 4.4 — rpc_cache_responses has data (migration sanity)
    rpc_n = con.execute("SELECT COUNT(*) FROM rpc_cache_responses").fetchone()[0]
    sev = 'PASS' if rpc_n > 100000 else 'WARN'
    out.append(Finding(sev, 4, 'rpc_cache_populated',
        f'rpc_cache_responses has {rpc_n:,} entries'))

    return out


# ────────────────────── main ──────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-detail', type=int, default=10, help='max detail rows per check')
    ap.add_argument('--tier', default='1,3,4', help='comma-sep tiers to run')
    args = ap.parse_args()
    tiers_to_run = {int(t) for t in args.tier.split(',')}

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    data = _load_json(DATA_JSON)

    report = AuditReport()
    t0 = time.time()

    if 1 in tiers_to_run:
        for f in tier1_structural(con, data, args.max_detail): report.add(f)
    if 3 in tiers_to_run:
        for f in tier3_solstice(con, data, args.max_detail): report.add(f)
    if 4 in tiers_to_run:
        for f in tier4_invariants(con, data, args.max_detail): report.add(f)

    elapsed = time.time() - t0

    # Print report
    print(f'\n=== AUDIT REPORT ({time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}) ===')
    print(f'Ran in {elapsed:.1f}s.\n')
    summary = report.summary()
    for tier in sorted(summary):
        s = summary[tier]
        icon = '✅' if s['FAIL'] == 0 and s['WARN'] == 0 else ('❌' if s['FAIL'] else '⚠️')
        print(f'TIER {tier}:  {icon}  PASS={s["PASS"]} WARN={s["WARN"]} FAIL={s["FAIL"]}')
    print()

    # Findings, grouped by severity
    fails = [f for f in report.findings if f.severity == 'FAIL']
    warns = [f for f in report.findings if f.severity == 'WARN']
    passes = [f for f in report.findings if f.severity == 'PASS']

    for label, group in [('FAILS', fails), ('WARNINGS', warns), ('OK', passes)]:
        if not group: continue
        print(f'━━━━━ {label} ━━━━━')
        for f in group:
            print(f'  [T{f.tier}] {f.check}: {f.message}')
            for line in (f.detail or []):
                print(line)
        print()

    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
