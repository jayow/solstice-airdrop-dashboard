"""S2_HOLD_EUSX_{DAILY,1MO,3MO}. Mirror of hold_usx but with eUSX mint, 2x/4x/10x
multipliers, and per-segment peg (eUSX appreciates over time, so a single
constant would systematically under- or over-count depending on snapshot age)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import QuestExtractor, S2_START_TS, S2_END_TS, load_quest_cache
from .hold_usx import _build_timeline, _integrate_twab, _integrate_min_balance_bonus, _hold_looks_empty, _hold_quick_validate
from .eusx_peg import peg_at

EUSX_MINT = "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC"


class HoldEUSXExtractor(QuestExtractor):
    QUEST_CODE = ("S2_HOLD_EUSX_DAILY", "S2_HOLD_EUSX_1MO", "S2_HOLD_EUSX_3MO")
    MULTIPLIER = {"S2_HOLD_EUSX_DAILY": 2, "S2_HOLD_EUSX_1MO": 4, "S2_HOLD_EUSX_3MO": 10}
    SHARED_CACHE_KEY = "S2_HOLD_EUSX"

    def extract(self, wallet: str) -> dict:
        prior = load_quest_cache(self.cache_key(), wallet)
        prior_atas = ((prior or {}).get("raw") or {}).get("atas") or []
        return _build_timeline(wallet, EUSX_MINT, int(time.time()), prior_atas)

    def looks_empty(self, raw: dict) -> bool:
        return _hold_looks_empty(raw)

    def quick_validate(self, wallet: str) -> bool:
        return _hold_quick_validate(wallet, EUSX_MINT)

    def transform(self, raw: dict, now_ts: int) -> dict:
        timeline = raw.get("timeline") or []
        end_ts = min(now_ts, S2_END_TS)
        # peg_at(ts) returns the per-second eUSX→USD rate, interpolated from
        # daily snapshots. Used as a callable inside the integrators.
        return {
            "S2_HOLD_EUSX_DAILY": _integrate_twab(timeline, 2, peg_at, end_ts),
            "S2_HOLD_EUSX_1MO":   _integrate_min_balance_bonus(timeline, 100.0, 30, 4, peg_at, end_ts),
            "S2_HOLD_EUSX_3MO":   _integrate_min_balance_bonus(timeline, 100.0, 90, 10, peg_at, end_ts),
        }
