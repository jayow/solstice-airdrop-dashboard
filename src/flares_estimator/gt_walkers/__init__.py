"""Per-quest ground-truth walkers.

Each module here owns exactly one S2 quest. The module:
  1. Hardcodes its on-chain source-of-truth address(es).
  2. Self-enumerates ALL relevant txs/accounts on that source during S2.
  3. Reconstructs per-wallet position timelines from raw on-chain data.
  4. Applies the quest's formula (with Solstice filters).
  5. Writes per-wallet flares to DB.walker_outputs (walker='gt_<quest_lower>').
  6. Reports a cross-check: our sum vs on-chain aggregate.

Convention:
  - Each module exposes a `run()` function that does the walk and returns
    dict({wallet: flares}).
  - Each module's `WALKER_NAME` constant is the row key in walker_outputs.
  - The driver `scripts/run_all_gt_walkers.sh` invokes them in parallel where safe.

Quest -> walker mapping is enumerated in `_registry.py`.
"""
