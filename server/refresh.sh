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

# ── Phase 2: Kamino strategy (depends on Kamino cache) ───────
echo "[$(date '+%H:%M:%S')] Phase 2: Kamino strategy backfill"
python3 src/flares_estimator/walk_s2_kamino_strategy.py > /tmp/walker_logs/refresh_kvault.log 2>&1
echo "[$(date '+%H:%M:%S')]   ✓ Kamino strategy done"

# ── Phase 3: transforms (event-integrated recomputes) ────────
echo "[$(date '+%H:%M:%S')] Phase 3: transforms"
python3 src/flares_estimator/transform_kamino.py    > /tmp/walker_logs/refresh_xform_kam.log 2>&1
python3 src/flares_estimator/transform_loopscale.py > /tmp/walker_logs/refresh_xform_loop.log 2>&1
echo "[$(date '+%H:%M:%S')]   ✓ Transforms done"

# ── Phase 4: rebuild dashboard ────────────────────────────────
echo "[$(date '+%H:%M:%S')] Phase 4: rebuild dashboard"
bash server/rebuild.sh 2>&1 | grep -E '^(\=\=\=|Reconstructed|wallet_quests|Solstice|Wrote)' | head -20

# Summary
END=$(date +%s)
echo "[$(date '+%H:%M:%S')] === Refresh complete in $((END - START)) seconds ==="
echo
python3 -c "
import json
data = json.load(open('server/data.json'))
records = data['records']
total_real = sum((r.get('total') or 0) for r in records if not r.get('is_protocol_pda'))
chart = json.load(open('server/daily_totals.json'))
SOL = 25497909198
print(f'Real users total: {total_real:>16,.0f}')
print(f'Chart last day:   {chart[\"days\"][-1][\"cumulative\"]:>16,.0f}')
print(f'Solstice published (snapshot Apr 13 → last 00:00 UTC): {SOL:>14,.0f}  (yesterday — will be updated when Solstice publishes new one)')
print(f'Gap real vs Solstice: {total_real - SOL:>+,.0f}  ({total_real / SOL * 100:.1f}%)')
"
echo
echo "Hard-refresh browser at http://localhost:8000 to see the new numbers."
