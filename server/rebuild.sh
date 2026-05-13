#!/bin/bash
# Single command to rebuild ALL dashboard data when wallet_quests changes.
# Run this after any walker/transform/classification update.
#
# Builds in dependency order:
#   1. data.json     — current snapshot (wallet_quests pivot)
#   2. daily_totals.json — chart cumulative-over-time (per-quest cache walks)
#
# Both must regenerate together so dashboard's "total" and "chart" stay in sync.

set -e
cd "$(dirname "$0")/.."

echo "=== Building data.json (current snapshot) ==="
python3 server/build_data.py | tail -5

echo
echo "=== Building daily_totals.json (chart) ==="
python3 server/build_daily_totals.py | tail -5

echo
echo "=== Building per-wallet detail JSONs (server/wallets/) ==="
python3 server/build_wallet_details.py | tail -3

echo
echo "=== Done. Hard-refresh browser at http://localhost:8000 to pick up changes ==="
