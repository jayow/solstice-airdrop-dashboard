#!/bin/bash
# End-to-end S2 flares refresh — run shortly after 00:00 UTC (08:00 SGT) to
# capture the just-completed Solstice daily snapshot.
#
# Order:
#   Phase 1: 6 walkers in parallel (LP, YT, Kamino, Loopscale, Orca, Raydium)
#   Phase 2 (after Kamino done): Kamino strategy (needs S2_KAMINO cache populated)
#   Phase 3: transforms (event-integrated recomputes)
#   Phase 4: rebuild dashboard (data.json + daily_totals.json together)
#
# Total runtime ~10-15 min depending on RPC throughput. Kamino is the slow path
# (per-obligation sig walks); everything else finishes earlier.
set -e
cd "$(dirname "$0")/.."
mkdir -p /tmp/walker_logs

START=$(date +%s)
echo "[$(date '+%H:%M:%S')] === Solstice S2 refresh start ==="

# ── Phase 1: walkers ────────────────────────────────────────
# REFRESH_MODE=ci → run sequentially. GitHub Actions runner can't sustain the
# parallel burst (100+ concurrent Helius connections) — silent RPC failures
# wipe most data. Local manual runs default to parallel (~4× faster).
REFRESH_MODE="${REFRESH_MODE:-parallel}"
if [ "$REFRESH_MODE" = "ci" ]; then
  echo "[$(date '+%H:%M:%S')] Phase 1: walkers SEQUENTIAL (REFRESH_MODE=ci)"
  for w in lp yt kamino loopscale orca raydium; do
    echo "[$(date '+%H:%M:%S')]   running walk_s2_$w.py"
    if python3 src/flares_estimator/walk_s2_$w.py > /tmp/walker_logs/refresh_$w.log 2>&1; then
      echo "[$(date '+%H:%M:%S')]     ✓ done"
    else
      echo "[$(date '+%H:%M:%S')]     ✗ FAILED"
    fi
  done
else
  echo "[$(date '+%H:%M:%S')] Phase 1: launching 6 walkers in PARALLEL"
  python3 src/flares_estimator/walk_s2_lp.py        > /tmp/walker_logs/refresh_lp.log 2>&1 &
  PID_LP=$!
  python3 src/flares_estimator/walk_s2_yt.py        > /tmp/walker_logs/refresh_yt.log 2>&1 &
  PID_YT=$!
  python3 src/flares_estimator/walk_s2_kamino.py    > /tmp/walker_logs/refresh_kamino.log 2>&1 &
  PID_KAM=$!
  python3 src/flares_estimator/walk_s2_loopscale.py > /tmp/walker_logs/refresh_loop.log 2>&1 &
  PID_LOOP=$!
  python3 src/flares_estimator/walk_s2_orca.py      > /tmp/walker_logs/refresh_orca.log 2>&1 &
  PID_ORCA=$!
  python3 src/flares_estimator/walk_s2_raydium.py   > /tmp/walker_logs/refresh_ray.log 2>&1 &
  PID_RAY=$!
  for name_pid in "LP:$PID_LP" "YT:$PID_YT" "Loopscale:$PID_LOOP" "Orca:$PID_ORCA" "Raydium:$PID_RAY" "Kamino:$PID_KAM"; do
    name="${name_pid%:*}"; pid="${name_pid#*:}"
    if wait $pid; then
      echo "[$(date '+%H:%M:%S')]   ✓ $name done"
    else
      echo "[$(date '+%H:%M:%S')]   ✗ $name FAILED — see /tmp/walker_logs/refresh_*.log"
    fi
  done
fi

# ── Phase 2: HOLD walkers (USX + eUSX, daily + 1MO + 3MO bonuses) ───
# These weren't in refresh.sh before, so HOLD flares went stale between
# manual runs. They share the S2_HOLD_USX / S2_HOLD_EUSX cache (TWAB timelines)
# so each pair fires the same 24h-cache check internally.
echo "[$(date '+%H:%M:%S')] Phase 2: HOLD walkers (USX + eUSX, 6 tiers)"
for w in gt_hold_usx_daily gt_hold_usx_1mo gt_hold_usx_3mo gt_hold_eusx_daily gt_hold_eusx_1mo gt_hold_eusx_3mo; do
  ( cd src && python3 -u -m flares_estimator.gt_walkers.$w ) > /tmp/walker_logs/refresh_$w.log 2>&1 &
done
wait
echo "[$(date '+%H:%M:%S')]   ✓ HOLD walkers done"

# ── Phase 3: Kamino strategy (depends on Kamino cache) ───────
echo "[$(date '+%H:%M:%S')] Phase 3: Kamino strategy backfill"
python3 src/flares_estimator/walk_s2_kamino_strategy.py > /tmp/walker_logs/refresh_kvault.log 2>&1
echo "[$(date '+%H:%M:%S')]   ✓ Kamino strategy done"

# ── Phase 4: transforms (event-integrated recomputes) ────────
echo "[$(date '+%H:%M:%S')] Phase 4: transforms"
python3 src/flares_estimator/transform_kamino.py    > /tmp/walker_logs/refresh_xform_kam.log 2>&1
python3 src/flares_estimator/transform_loopscale.py > /tmp/walker_logs/refresh_xform_loop.log 2>&1
echo "[$(date '+%H:%M:%S')]   ✓ Transforms done"

# ── Phase 5: HOLD cache → wallet_quests resync ───────────────
# When a walker rebuilds a cache (e.g. force-refresh after stale-cache detection),
# the wallet_quests rows for the 1MO/3MO bonus tiers can lag. Bonus-tier values
# in wallet_quests are written by the bonus walker (gt_hold_*_1mo), which only
# writes for wallets it CURRENTLY computes as qualifying — leaving stale 0s for
# wallets whose cache was repaired AFTER the bonus walker ran. This resync
# pass iterates every cache with positive timeline and writes fresh DAILY +
# 1MO + 3MO values so wallet_quests reflects the cache exactly.
echo "[$(date '+%H:%M:%S')] Phase 5: HOLD cache → wallet_quests resync"
python3 tools/resync_hold_quests.py > /tmp/walker_logs/refresh_resync.log 2>&1 || echo "  ⚠️  resync errored — see refresh_resync.log"
echo "[$(date '+%H:%M:%S')]   ✓ Resync done"

# ── Phase 6: rebuild dashboard ────────────────────────────────
echo "[$(date '+%H:%M:%S')] Phase 6: rebuild dashboard"
bash server/rebuild.sh 2>&1 | grep -E '^(\=\=\=|Reconstructed|wallet_quests|Solstice|Wrote)' | head -20

# ── Phase 6b: walker cursor-freshness sampling ───────────────
# Cheap (one RPC per sample, force_refresh) but catches the deepest class
# of bug: walker cursor logic silently stops tracking new sigs.
echo "[$(date '+%H:%M:%S')] Phase 6b: walker cursor freshness"
python3 tools/walker_cursor_freshness.py --n 5 > /tmp/walker_logs/refresh_freshness.log 2>&1 || \
  echo "  ⚠️  freshness check errored — see refresh_freshness.log"
echo "[$(date '+%H:%M:%S')]   ✓ Freshness check done"

# ── Phase 7: audit (fail loudly on structural drift) ─────────
echo "[$(date '+%H:%M:%S')] Phase 7: audit"
if python3 tools/audit.py 2>&1 | tee /tmp/walker_logs/refresh_audit.log | tail -25; then
  echo "[$(date '+%H:%M:%S')]   ✓ Audit clean"
else
  echo "[$(date '+%H:%M:%S')]   ❌ AUDIT FAILED — see refresh_audit.log. DO NOT PUSH."
  # Continue to print summary but mark the refresh as suspect.
fi

# Summary
END=$(date +%s)
echo "[$(date '+%H:%M:%S')] === Refresh complete in $((END - START)) seconds ==="
echo
python3 -c "
import json, sqlite3
data = json.load(open('server/data.json'))
records = data['records']
total_real = sum((r.get('total') or 0) for r in records if not r.get('is_protocol_pda'))
chart = json.load(open('server/daily_totals.json'))
# Pull Solstice's latest published total from DB (source of truth).
# Update via: python3 tools/set_solstice_total.py <total>
con = sqlite3.connect('data/solstice.db')
row = con.execute(\"SELECT date_utc, grand_total FROM flares_snapshots \"
                  \"WHERE source='solstice_dashboard' ORDER BY ts DESC LIMIT 1\").fetchone()
if row:
    sol_date, SOL = row
    print(f'Real users total: {total_real:>16,.0f} flares')
    print(f'Chart last day:   {chart[\"days\"][-1][\"cumulative\"]:>16,.0f} flares')
    print(f'Solstice published @ {sol_date}: {SOL:>16,.0f} flares')
    print(f'Gap real vs Solstice: {total_real - SOL:>+,.0f}  ({total_real / SOL * 100:.2f}%)')
else:
    print('⚠️  no solstice_dashboard snapshot in DB — run tools/set_solstice_total.py <total>')
    print(f'Real users total: {total_real:>16,.0f} flares')
"
echo
echo "Hard-refresh browser at http://localhost:8000 to see the new numbers."
