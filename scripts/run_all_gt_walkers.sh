#!/usr/bin/env bash
# Run all 24 ground-truth walkers. Each writes to DB.walker_outputs + syncs to wallet_quests.
#
# Pass --light to skip the slow HOLD/Kamino enumerations (uses cache-only mode).
# Pass --full to force-refresh every walker.

set -uo pipefail
cd "$(dirname "$0")/.."
LOG="data/gt_walkers.log"
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }

MODE="${1:-default}"

log "==== START run_all_gt_walkers (mode=$MODE) ===="

# Run walkers in groups (parallel within group, sequential across groups for RPC safety)
run_walker() {
  local w="$1"
  log "  → $w"
  ( cd src && python3 -u -m flares_estimator.gt_walkers."$w" ) 2>&1 | tee -a "$LOG"
}

# Group 1: light wrapper walkers that just read from existing .json files (instant)
log "[1/5] light wrapper walkers (file-based reads)"
for w in gt_exponent_lp_usx_jun26 gt_exponent_lp_eusx_jun26 \
          gt_kamino_lend_usx gt_kamino_lend_eusx gt_kamino_lend_usdg \
          gt_kamino_borrow_usx gt_kamino_borrow_usdg \
          gt_kamino_kvault_usdg_usx \
          gt_loopscale_borrow_usx gt_loopscale_supply_usx_one \
          gt_orca_usx_usdc gt_orca_eusx_usx gt_orca_usx_usdg \
          gt_raydium_usx_usdc gt_raydium_eusx_usx \
          gt_referral_bonus; do
  run_walker "$w"
done

# Group 2: YT walkers (read from cache, fast)
log "[2/5] Exponent YT walkers (cache-based)"
for w in gt_exponent_yield_usx_jun26 gt_exponent_yield_eusx_jun26; do
  run_walker "$w"
done

# Group 3: HOLD walkers — heavier (full SPL enumeration). Run in parallel.
log "[3/5] HOLD walkers (full SPL enumeration, slower)"
if [ "$MODE" != "--light" ]; then
  for w in gt_hold_usx_daily gt_hold_usx_1mo gt_hold_usx_3mo \
            gt_hold_eusx_daily gt_hold_eusx_1mo gt_hold_eusx_3mo; do
    run_walker "$w"
  done
fi

# Group 4: rebuild dashboard from DB
log "[4/5] rebuild server/data.json from DB"
python3 -u server/build_data.py 2>&1 | tail -30 | tee -a "$LOG"

# Group 5: summary
log "[5/5] summary"
python3 -u <<'PY' 2>&1 | tee -a "$LOG"
import sys
sys.path.insert(0, 'src/flares_estimator')
import db
db.init()
c = db.conn()
rows = c.execute("""
    SELECT walker, quest, COUNT(*) as n, SUM(flares) as total
    FROM walker_outputs WHERE walker LIKE 'gt_%' GROUP BY walker, quest ORDER BY total DESC
""").fetchall()
print(f'{"walker":<32s}{"quest":<35s}{"n_wallets":>10s}{"total":>20s}')
print('-' * 100)
total_sum = 0
for r in rows:
    print(f'{r["walker"]:<32s}{r["quest"]:<35s}{r["n"]:>10,}{r["total"]:>20,.0f}')
    total_sum += r['total']
print('-' * 100)
print(f'{"GRAND TOTAL":<67s}{total_sum:>20,.0f}')
PY

log "==== DONE run_all_gt_walkers ===="
