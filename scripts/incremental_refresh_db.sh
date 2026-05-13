#!/usr/bin/env bash
# DB-backed incremental refresh — designed to complete in ~5-15 min.
#
# Architecture:
#   1. Find wallets active on-chain in last N hours (default 24h) via signature scan
#   2. For each active wallet: force-refresh via orchestrator → DB.wallet_quests + DB.quest_cache
#   3. Re-transform every wallet in DB.wallet_quests with new now_ts (no RPC)
#   4. Re-run pool-state walkers (self-enumerate from on-chain → DB.walker_outputs)
#   5. Sync walker_outputs → wallet_quests for walker-owned quests
#   6. Rebuild server/data.json from DB
#
# Per-wallet atomic — never overwrites OTHER wallets' data.

set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LOG="$ROOT/data/refresh.log"
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }

SINCE_HOURS="${1:-24}"
ACTIVE_FILE="$ROOT/data/active_wallets_recent.txt"

log "==== START incremental_refresh_db (since ${SINCE_HOURS}h) ===="

# 1. Find active wallets
log "[1/5] discovering active wallets in last ${SINCE_HOURS}h"
python3 -u src/flares_estimator/find_active_wallets.py --since-hours "$SINCE_HOURS" > "$ACTIVE_FILE" 2>>"$LOG"
N=$(wc -l < "$ACTIVE_FILE" | tr -d ' ')
log "      $N active wallets"

# 2. Force-refresh only active wallets (writes DB.quest_cache + DB.wallet_quests)
if [ "$N" -gt 0 ]; then
  log "[2/5] force-refreshing $N active wallets"
  ( cd src && python3 -u -m flares_estimator.quests.orchestrator \
      --bulk "$ACTIVE_FILE" --workers 16 --force-refresh ) 2>&1 | tail -5 | tee -a "$LOG"
else
  log "[2/5] no active wallets — skipping force-refresh"
fi

# 3. Re-transform every wallet in DB.wallet_quests with new now_ts (recomputes
#    time-extended quests like YT/HOLD without re-extracting).
log "[3/5] re-transforming wallet_quests with new now_ts"
python3 -u <<'PY' 2>&1 | tail -5 | tee -a "$LOG"
import os, sys, time, json
sys.path.insert(0, 'src/flares_estimator')
import db
from quests.orchestrator import transform_wallet_from_cache, NON_EXTRACTABLE_QUESTS, GATED_OFF_QUESTS

db.init()
now_ts = int(time.time())
WALKER_QUESTS = {
    'S2_EXPONENT_LP_USX_JUN26','S2_EXPONENT_LP_EUSX_JUN26',
    'S2_KAMINO_LEND_USX','S2_KAMINO_LEND_EUSX','S2_KAMINO_LEND_USDG',
    'S2_KAMINO_BORROW_USX','S2_KAMINO_BORROW_USDG','S2_KAMINO_KVAULT_USDG_USX',
    'S2_LOOPSCALE_BORROW_USX','S2_LOOPSCALE_SUPPLY_USX_ONE',
    'S2_ORCA_USX_USDC','S2_ORCA_EUSX_USX','S2_ORCA_USX_USDG',
    'S2_RAYDIUM_USX_USDC','S2_RAYDIUM_EUSX_USX',
}

# Iterate every wallet that has cache or wallet_quests
wallets = set()
for r in db.conn().execute("SELECT DISTINCT wallet FROM quest_cache UNION SELECT DISTINCT wallet FROM wallet_quests"):
    wallets.add(r['wallet'])
print(f'transforming {len(wallets):,} wallets at now_ts={now_ts}')

t0 = time.time()
with db.txn() as c:
    for i, w in enumerate(wallets):
        if i and i % 5000 == 0: print(f'  {i}/{len(wallets)}  ({time.time()-t0:.1f}s)')
        flares = transform_wallet_from_cache(w, now_ts) or {}
        # Don't touch walker_quests — those come from walker_outputs (next step)
        for quest, val in flares.items():
            if quest in WALKER_QUESTS: continue
            c.execute(
                'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (w, quest, float(val), 'transform', now_ts)
            )
print(f'done in {time.time()-t0:.1f}s')
PY

# 4. Re-run walkers (LP, Kamino, Strategy, Loopscale, Orca, Raydium)
log "[4/5] running pool-state walkers"
for walker in walk_s2_lp walk_s2_kamino walk_s2_kamino_strategy walk_s2_loopscale walk_s2_orca walk_s2_raydium; do
  log "    $walker"
  python3 -u "src/flares_estimator/$walker.py" 2>&1 | tail -3 | tee -a "$LOG"
done

# 5. Sync walker_outputs → wallet_quests for walker-authoritative quests
log "[5/5] sync walker_outputs -> wallet_quests"
python3 -u <<'PY' 2>&1 | tail -5 | tee -a "$LOG"
import os, sys
sys.path.insert(0, 'src/flares_estimator')
import walker_db
# Each walker writes (walker_name, quest_list)
walkers = [
    ('walk_s2_lp', ['S2_EXPONENT_LP_USX_JUN26','S2_EXPONENT_LP_EUSX_JUN26']),
    ('walk_s2_kamino', ['S2_KAMINO_LEND_USX','S2_KAMINO_LEND_EUSX','S2_KAMINO_LEND_USDG','S2_KAMINO_BORROW_USX','S2_KAMINO_BORROW_USDG']),
    ('walk_s2_kamino_strategy', ['S2_KAMINO_KVAULT_USDG_USX']),
    ('walk_s2_loopscale', ['S2_LOOPSCALE_BORROW_USX','S2_LOOPSCALE_SUPPLY_USX_ONE']),
    ('walk_s2_orca', ['S2_ORCA_USX_USDC','S2_ORCA_EUSX_USX','S2_ORCA_USX_USDG']),
    ('walk_s2_raydium', ['S2_RAYDIUM_USX_USDC','S2_RAYDIUM_EUSX_USX']),
]
for w, qs in walkers:
    walker_db.sync_to_wallet_quests(w, qs)
print(f'synced {len(walkers)} walkers')
PY

# 6. Build dashboard from DB
log "[6/6] rebuilding server/data.json from DB"
python3 -u server/build_data_db.py 2>&1 | tail -25 | tee -a "$LOG"

log "==== DONE incremental_refresh_db ===="
