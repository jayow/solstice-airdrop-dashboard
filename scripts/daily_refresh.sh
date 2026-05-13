#!/usr/bin/env bash
# Daily Solstice S2 flares refresh — designed to run at 00:00 UTC (08:00 MYT).
#
# Pipeline:
#   1. Discover universe (all on-chain holders + protocol participants)
#   2. Run all S2 history walkers (Exponent LP, Kamino lending+strategy, Loopscale, Orca, Raydium)
#   3. Run HOLD-quest extractor for every wallet in universe
#   4. Merge all walk outputs into quest_results.jsonl
#   5. Rebuild flares_stage3 → filter_pdas → server/data.json
#   6. Log summary
#
# Outputs:
#   data/universe_today.txt       — fresh universe each run
#   data/universe_snapshots.jsonl — per-day stats
#   data/refresh.log              — append-only run log
#
# Schedule (macOS launchd, or cron):
#   0 0 * * *  /path/to/SolsticeAirdropUsers/scripts/daily_refresh.sh

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LOG="$ROOT/data/refresh.log"
TS() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "[$(TS)] $*" | tee -a "$LOG"; }

log "=== START daily_refresh ==="

# 1. Discover universe
log "[1/6] discover_universe"
python3 src/flares_estimator/discover_universe.py 2>&1 | tee -a "$LOG"

# 2. Run history walkers (each writes to its own data/s2_*.json)
log "[2/6] walkers"
python3 src/flares_estimator/walk_s2_lp.py            2>&1 | tee -a "$LOG"
python3 src/flares_estimator/walk_s2_kamino.py        2>&1 | tee -a "$LOG"
python3 src/flares_estimator/walk_s2_kamino_strategy.py 2>&1 | tee -a "$LOG"
python3 src/flares_estimator/walk_s2_loopscale.py     2>&1 | tee -a "$LOG"
python3 src/flares_estimator/walk_s2_orca.py          2>&1 | tee -a "$LOG"
python3 src/flares_estimator/walk_s2_raydium.py       2>&1 | tee -a "$LOG"

# 3. Run HOLD + YT for every wallet in fresh universe
#    (bulk runner uses persistent cache → fast re-run for unchanged wallets)
log "[3/6] orchestrator bulk over fresh universe"
( cd src && python3 -u -m flares_estimator.quests.orchestrator \
        --bulk ../data/universe_today.txt \
        --workers 16 ) 2>&1 | tee -a "$LOG"

# 3.5. Retry wallets whose HOLD/YT caches look empty. Catches users whose
#      previous extract hit a transient RPC failure and got an empty result
#      cached — without this step, those wallets stay stuck at zero forever
#      because subsequent runs short-circuit on "cache exists → skip extract".
log "[3.5/7] retry empty caches (self-healing)"
python3 src/flares_estimator/retry_empty_caches.py --workers 4 2>&1 | tee -a "$LOG"

# 4. Re-apply improved transforms (event-integrated Kamino/Loopscale,
#    CLMM in-range gating). These overwrite the walker's approximations
#    in wallet_quests with proper time-integrated / in-range-gated values.
log "[4/7] post-walker transforms"
python3 src/flares_estimator/transform_kamino.py        2>&1 | tee -a "$LOG"
python3 src/flares_estimator/transform_loopscale.py     2>&1 | tee -a "$LOG"
python3 src/flares_estimator/transform_clmm_inrange.py  2>&1 | tee -a "$LOG"
python3 src/flares_estimator/transform_clmm_das.py      2>&1 | tee -a "$LOG"

# 5. Rebuild dashboard data
log "[5/7] rebuild dashboard"
python3 src/flares_estimator/quests/build_stage3.py 2>&1 | tee -a "$LOG"
python3 src/flares_estimator/filter_pdas_db.py      2>&1 | tee -a "$LOG"
python3 server/build_data.py                        2>&1 | tee -a "$LOG"
python3 server/build_wallet_details.py              2>&1 | tee -a "$LOG"
python3 server/build_daily_totals.py                2>&1 | tee -a "$LOG"

# 6. Summary
log "[6/7] summary"
python3 - <<'PY' | tee -a "$LOG"
import json, os
p = 'server/data.json'
d = json.load(open(p))
print(f'  records: {len(d["records"]):,}')
print(f'  grand_total: {sum(d["quest_totals"].values()):,.0f}')
print(f'  top quest: {max(d["quest_totals"].items(), key=lambda x:x[1])}')
PY

log "=== END daily_refresh ==="
