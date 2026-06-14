"""Build server-s1/data.json from data/solstice.db slx_allocations table."""
import sqlite3, json, os, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')
OUT = os.path.join(ROOT, 'server-s1', 'data.json')

con = sqlite3.connect(DB)
cur = con.cursor()

# Pull real allocation rows (skip _NONE_ markers)
cur.execute("""
  SELECT a.wallet, w.cohort, w.classification,
         a.batch, a.total_slx, a.liquid_slx, a.vested_slx,
         a.amt_standard, a.amt_exponent, a.amt_flares_bonus, a.amt_presale,
         a.schedule_code, a.claim_at
  FROM slx_allocations a JOIN wallets w ON w.wallet=a.wallet
  WHERE a.batch != '_NONE_' AND w.is_s1=1
  ORDER BY a.total_slx DESC
""")
rows = []
# Latest balance snapshot per wallet (now includes stSLX split)
cur.execute("""
  SELECT s.wallet, s.balance_slx, s.balance_stslx, s.delta_pct, s.category, s.ts
  FROM slx_balance_snapshots s
  WHERE s.ts = (SELECT MAX(ts) FROM slx_balance_snapshots WHERE wallet=s.wallet)
""")
latest_bal = {r[0]: r[1:] for r in cur.fetchall()}

# Pull legion_buyers — used to tag PRESALE / PRESALE_PENDING based on whether they actually claimed SLX
cur.execute("SELECT wallet, net_usdc FROM legion_buyers WHERE net_usdc > 0.01")
legion_net_buyers = {r[0]: r[1] for r in cur.fetchall()}
# Wallets that actually received Legion SLX (= claimed) via the on-chain distributor
cur.execute("SELECT DISTINCT recipient FROM slx_legion_distributions")
legion_claimed = {r[0] for r in cur.fetchall()}

# Roll up per wallet
cur.execute("""
  SELECT a.wallet, w.cohort, w.classification, w.is_s1,
         a.batch, a.total_slx, a.liquid_slx, a.vested_slx,
         a.amt_standard, a.amt_exponent, a.amt_flares_bonus, a.amt_presale,
         COALESCE(a.amt_xeet, 0), COALESCE(a.amt_rivalz, 0),
         a.schedule_code, a.claim_at, a.vest_track
  FROM slx_allocations a
  LEFT JOIN wallets w ON w.wallet = a.wallet
  WHERE a.batch != '_NONE_'
""")
agg = {}
for r in cur.fetchall():
    w, coh, cls, is_s1, batch, tot, liq, vest, std, exp, bn, ps, xt, rv, sc, ca, vt = r
    a = agg.setdefault(w, {
        'w': w, 'c': coh or '', 'cls': cls, 'is_s1': bool(is_s1),
        't':0, 'l':0, 'v':0, 'std':0, 'exp':0, 'bn':0, 'ps':0, 'xt':0, 'rv':0,
        'sc': '', 'ca': '', 'vt': '', 'tags': set(),
    })
    a['t']   += tot or 0
    a['l']   += liq or 0
    a['v']   += vest or 0
    a['std'] += std or 0
    a['exp'] += exp or 0
    a['bn']  += bn or 0
    a['ps']  += ps or 0
    a['xt']  += xt or 0
    a['rv']  += rv or 0
    if sc and not a['sc']: a['sc'] = sc
    if ca and (not a['ca'] or ca < a['ca']): a['ca'] = ca   # earliest claim
    if vt and not a['vt']: a['vt'] = vt
    # Add tag for batch
    if batch == 'STR9e352c':                  a['tags'].add('S1')
    if batch == 'LEGION_PRESALE':             a['tags'].add('PRESALE')

# Tag Legion net-buyers as PRESALE (if they actually claimed SLX) or PRESALE_PENDING (still owed).
# Also add minimal rows for wallets that paid USDC but aren't yet in slx_allocations.
for w, usdc in legion_net_buyers.items():
    claimed = w in legion_claimed
    if w not in agg:
        # Bare row — they paid USDC but haven't claimed any SLX yet
        agg[w] = {
            'w': w, 'c': '', 'cls': None, 'is_s1': False,
            't':0, 'l':0, 'v':0, 'std':0, 'exp':0, 'bn':0, 'ps':0, 'xt':0, 'rv':0,
            'sc': '', 'ca': '', 'tags': {'PRESALE' if claimed else 'PRESALE_PENDING'},
            'legion_usdc': round(usdc, 2),
        }
    else:
        agg[w]['tags'].add('PRESALE' if claimed else 'PRESALE_PENDING')
        agg[w]['legion_usdc'] = round(usdc, 2)

# Decorate with behavior + finalize
for w, a in agg.items():
    # Add behavior tag based on presence of source amounts (post-rollup)
    bal_info = latest_bal.get(w)
    if bal_info:
        bal_slx, bal_stslx, dpct, cat, snap_ts = bal_info
        bal_slx = bal_slx or 0; bal_stslx = bal_stslx or 0
        total_now = bal_slx + bal_stslx
        bought = max(0, total_now - a['l'])
        sold = max(0, a['l'] - total_now)
    else:
        bal_slx = bal_stslx = dpct = cat = bought = sold = snap_ts = None
    # Auxiliary tags
    if a['xt'] > 0:  a['tags'].add('XEET')
    if a['rv'] > 0:  a['tags'].add('RIVALZ')
    if a['exp'] > 0: a['tags'].add('EXP')   # had Exponent V2 farming bonus

    # Compute estimated Unlock 2 from vested using the on-chain formulas:
    #   3m plan: vested × 31/92 ≈ 33.70%
    #   9m plan: vested × 31/275 ≈ 11.27%
    # vest_track column (from tools/refresh_vest_track.py via Clique Merkle-proof
    # validation) tells us each wallet's actual selection. Both numbers are still
    # emitted so the UI can show the alternate "what-if" estimate.
    u2_3m = round(a['v'] * 31 / 92, 4)
    u2_9m = round(a['v'] * 31 / 275, 4)
    rows.append({
        'w': w, 'c': a['c'], 'cls': a['cls'],
        'is_s1': a['is_s1'],
        'tags': sorted(a['tags']),
        'usdc': a.get('legion_usdc'),
        'u2_3m': u2_3m,
        'u2_9m': u2_9m,
        't':  round(a['t'], 4),
        'l':  round(a['l'], 4),
        'v':  round(a['v'], 4),
        'std': round(a['std'], 4),
        'exp': round(a['exp'], 4),
        'bn':  round(a['bn'], 4),
        'ps':  round(a['ps'], 4),
        'xt':  round(a['xt'], 4),
        'rv':  round(a['rv'], 4),
        'sc': a['sc'],
        'ca': a['ca'],
        # vest_track: '3mo' (selected), '9mo_default' (didn't select, defaults to 9mo),
        # '9mo_selected' (explicitly chose 9mo — none seen yet), 'u1_only' (rare),
        # '' (unclaimed or not yet classified). Empty for unclaimed wallets by design.
        'vt': a['vt'],
        'held':   round(bal_slx, 4)   if bal_slx   is not None else None,
        'staked': round(bal_stslx, 4) if bal_stslx is not None else None,
        'bought': round(bought, 4)    if bought    is not None else None,
        'sold':   round(sold, 4)      if sold      is not None else None,
        'dpct':   round(dpct, 2)      if dpct      is not None else None,
        'cat':    cat,
        # snap_ts: epoch seconds of this wallet's latest slx_balance_snapshots row.
        # None when the wallet has no snapshot (typically never-claimed presale-only rows).
        # Used by the dashboard's per-row 'Updated' column.
        'snap_ts': snap_ts,
    })

# Sort by total desc
rows.sort(key=lambda r: -r['t'])
# Cohort field on each row is preserved from our DB — represents the Clique S1 REGISTRATION batch
# the wallet registered through (not allocation rank). Wallets that didn't go through batched
# registration show no cohort (empty) but may still have allocations.

# Aggregate stats
cur.execute("SELECT COUNT(*) FROM wallets WHERE is_s1=1")
total_s1_in_db = cur.fetchone()[0]
TOTAL_S1_OFFICIAL = 14895   # from Solstice's official Unlock-1 table (10+40+150+800+13895)
cur.execute("SELECT COUNT(DISTINCT wallet) FROM slx_allocations WHERE batch != '_NONE_'")
fetched_wallets = cur.fetchone()[0]
cur.execute("""
  SELECT SUM(total_slx), SUM(liquid_slx), SUM(vested_slx),
         SUM(amt_standard), SUM(amt_exponent), SUM(amt_flares_bonus), SUM(amt_presale),
         SUM(CASE WHEN claim_at != '' AND claim_at IS NOT NULL THEN total_slx ELSE 0 END),
         SUM(CASE WHEN claim_at != '' AND claim_at IS NOT NULL THEN 1 ELSE 0 END),
         COALESCE(SUM(amt_xeet),0), COALESCE(SUM(amt_rivalz),0)
  FROM slx_allocations WHERE batch != '_NONE_'
""")
sums = cur.fetchone()

# Cohort breakdown (within fetched)
cur.execute("""
  SELECT w.cohort, COUNT(*) as n,
         SUM(a.total_slx) as tot, SUM(a.liquid_slx) as liq, AVG(a.total_slx) as avg_a,
         MAX(a.total_slx) as max_a, MIN(a.total_slx) as min_a,
         SUM(CASE WHEN a.claim_at != '' AND a.claim_at IS NOT NULL THEN 1 ELSE 0 END) as claimed
  FROM slx_allocations a JOIN wallets w ON w.wallet=a.wallet
  WHERE a.batch != '_NONE_' AND w.is_s1=1
  GROUP BY w.cohort
  ORDER BY w.cohort
""")
cohorts = []
for c, n, tot, liq, avg_a, max_a, min_a, claimed in cur.fetchall():
    cohorts.append({
        'cohort': c or '(none)',
        'n': n,
        'total': round(tot or 0, 2),
        'liquid': round(liq or 0, 2),
        'avg': round(avg_a or 0, 2),
        'max': round(max_a or 0, 2),
        'min': round(min_a or 0, 2),
        'claimed_count': claimed,
        'claimed_pct': round(claimed/n*100, 1) if n else 0,
    })

# Total cohort population from wallets table
cur.execute("""
  SELECT cohort, COUNT(*) FROM wallets WHERE is_s1=1 GROUP BY cohort ORDER BY cohort
""")
cohort_population = {(c or '(none)'): n for c, n in cur.fetchall()}

# Holding-behavior breakdown (latest snapshot)
cur.execute("""
  WITH latest AS (
    SELECT s.* FROM slx_balance_snapshots s
    WHERE s.ts = (SELECT MAX(ts) FROM slx_balance_snapshots WHERE wallet=s.wallet)
  )
  SELECT category, COUNT(*), SUM(balance_slx), SUM(liquid_claimed)
  FROM latest GROUP BY category
""")
behavior = {}
for cat, n, bal, liq in cur.fetchall():
    behavior[cat] = {
        'count':         n,
        'balance_now':   round(bal or 0, 2),
        'liquid_total':  round(liq or 0, 2),
    }
cur.execute("SELECT MAX(ts) FROM slx_balance_snapshots")
last_snap = cur.fetchone()[0]

# Vest-track aggregate: how many claimed wallets picked each plan.
# Source: slx_allocations.vest_track (written by tools/refresh_vest_track.py)
cur.execute("""
  SELECT COALESCE(vest_track,''), COUNT(DISTINCT wallet), ROUND(SUM(vested_slx), 2)
  FROM slx_allocations
  WHERE claim_at != '' AND batch != '_NONE_'
  GROUP BY COALESCE(vest_track,'')
""")
vest_split = {}
for vt, n, vested in cur.fetchall():
    key = vt or 'pending_classification'
    vest_split[key] = {'count': n, 'vested_slx': vested or 0}

# Count S1-tagged and PRESALE-tagged wallets (post-rollup)
n_s1      = sum(1 for r in rows if 'S1' in r['tags'])
n_presale_claimed = sum(1 for r in rows if 'PRESALE' in r['tags'])
n_presale_pending = sum(1 for r in rows if 'PRESALE_PENDING' in r['tags'])
n_presale_total   = n_presale_claimed + n_presale_pending   # all Legion net-buyers
n_presale_only    = sum(1 for r in rows if ('PRESALE' in r['tags'] or 'PRESALE_PENDING' in r['tags']) and 'S1' not in r['tags'])
n_s1_only         = sum(1 for r in rows if 'S1' in r['tags'] and 'PRESALE' not in r['tags'] and 'PRESALE_PENDING' not in r['tags'])
n_both            = sum(1 for r in rows if 'S1' in r['tags'] and ('PRESALE' in r['tags'] or 'PRESALE_PENDING' in r['tags']))

stats = {
    'total_s1_wallets_in_db':   total_s1_in_db,
    'total_s1_official':         TOTAL_S1_OFFICIAL,
    'fetched_wallets':           fetched_wallets,
    'fetched_pct':               round(fetched_wallets/TOTAL_S1_OFFICIAL*100, 2) if TOTAL_S1_OFFICIAL else 0,
    'n_s1':                      n_s1,
    'n_presale_claimed':         n_presale_claimed,
    'n_presale_pending':         n_presale_pending,
    'n_presale_total':           n_presale_total,
    'n_presale_only':            n_presale_only,
    'n_s1_only':                 n_s1_only,
    'n_both':                    n_both,
    'sum_total_alloc':           round(sums[0] or 0, 2),
    'sum_liquid_unlock1':        round(sums[1] or 0, 2),
    'sum_vested':                round(sums[2] or 0, 2),
    'sum_amt_standard':          round(sums[3] or 0, 2),
    'sum_amt_exponent':          round(sums[4] or 0, 2),
    'sum_amt_flares_bonus':      round(sums[5] or 0, 2),
    'sum_amt_presale':           round(sums[6] or 0, 2),
    'sum_amt_xeet':              round(sums[9] or 0, 2),
    'sum_amt_rivalz':            round(sums[10] or 0, 2),
    'sum_total_claimed':         round(sums[7] or 0, 2),
    'count_claimed':             sums[8] or 0,
    'sum_u2_3m':                 round(sum(r.get('u2_3m',0) for r in rows), 2),
    'sum_u2_9m':                 round(sum(r.get('u2_9m',0) for r in rows), 2),
    'claimed_pct_by_count':      round((sums[8] or 0)/fetched_wallets*100, 2) if fetched_wallets else 0,
    'claimed_pct_by_alloc':      round((sums[7] or 0)/(sums[0] or 1)*100, 2) if sums[0] else 0,
    'cohort_population_total_db': cohort_population,
    'behavior':                  behavior,
    'last_balance_snapshot_ts':  last_snap,
    # vest_split: per-track count + vested SLX. Keys: '3mo' (selected),
    # '9mo_default' (didn't select; will default to 9mo), '9mo_selected'
    # (rare), 'u1_only', 'unknown', 'error', 'pending_classification' (not
    # yet walked). See tools/refresh_vest_track.py for the source of truth.
    'vest_split':                vest_split,
}

# Additional freshness signals for the dashboard's "Updated" column / as-of line.
# allocations_last_fetched_at: max(fetched_at) across slx_allocations — when
# the allocation universe (Clique acs-v4 API) was last swept.
cur.execute("SELECT MAX(fetched_at) FROM slx_allocations")
_alloc_fetched_max = cur.fetchone()[0]

doc = {
    'meta': {
        'generated_at': int(time.time()),
        'source':       'on-chain (Solana) + Clique acs-v4 allocations API',
        'deployment':   '019e588b-67b6-742f-af98-104cbb6a425c',
        'app_id':       '85da4f0c',
        # Freshness signals — used by server-s1/index.html to render
        # "Snapshots as of …" + the per-row Updated column.
        'allocations_last_fetched_at': _alloc_fetched_max,
        'balance_snapshot_last_ts':    last_snap,
    },
    'stats':    stats,
    'cohorts':  cohorts,
    'rows':     rows,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w') as f:
    json.dump(doc, f, separators=(',',':'))
print(f"Wrote {OUT}")
print(f"  rows: {len(rows):,}  size: {os.path.getsize(OUT):,} bytes")
print(f"  fetched: {stats['fetched_wallets']:,} / {stats['total_s1_official']:,} (official) = {stats['fetched_pct']}%")
print(f"  sum total: {stats['sum_total_alloc']:,.2f} SLX")
print(f"  sum liquid: {stats['sum_liquid_unlock1']:,.2f} SLX")
print(f"  claimed: {stats['count_claimed']:,} wallets ({stats['claimed_pct_by_count']}%)")
