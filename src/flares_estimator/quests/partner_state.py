"""
Partner-state ELT module — wraps the existing extractors that already cache
via rpc_helper.rpc(). Covers Kamino/Orca/Raydium/Loopscale (13 quests total).

These quests use a current-state × days_in_S2 model. Less precise than the
HOLD-TWAB / Exponent-YT history walks, but the existing extractors already
produce stable output and their RPC calls are cached. To upgrade any of these
to full timeline reconstruction later, write a dedicated module per quest
following the hold_usx.py / exponent_yt.py pattern.
"""
import os, sys, time, base64, struct
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kamino_extractor import get_kamino_positions
from orca_extractor import get_orca_lp_positions
from raydium_extractor import get_raydium_lp_positions
from loopscale_extractor import get_loopscale_positions
from rpc_helper import rpc
from .base import QuestExtractor, S2_START_TS, S2_END_TS

# Kamino API's marketValueSf field is already USD-denominated (priced by Kamino
# using their internal feed). The extractor stores these USD values directly, so
# per-unit USD multipliers below should all be 1.0 (no peg re-application).
# The eUSX peg PDA exists at JDs1wmLaVB2KsAotjbBKVEsiV1gbrG3Qrjyht5LnX9YP[48]
# (currently $1.156) if we ever need it elsewhere — but NOT here.

# (extractor_field, quest_code, multiplier, usd_per_unit)
_KAMINO = [
    ("kamino_supply_usx",       "S2_KAMINO_LEND_USX",         5,  1.0),
    ("kamino_supply_eusx",      "S2_KAMINO_LEND_EUSX",        1,  1.0),
    ("kamino_supply_usdg",      "S2_KAMINO_LEND_USDG",        5,  1.0),
    ("kamino_borrow_usx",       "S2_KAMINO_BORROW_USX",       1,  1.0),
    ("kamino_borrow_usdg",      "S2_KAMINO_BORROW_USDG",      1,  1.0),
    ("kamino_kvault_usx_usdg",  "S2_KAMINO_KVAULT_USDG_USX", 10,  1.0),
]
_ORCA = [
    ("orca_usx_usdc",  "S2_ORCA_USX_USDC",  9, 1.0),
    ("orca_eusx_usx",  "S2_ORCA_EUSX_USX",  4, 1.0),  # already in USD
    ("orca_usx_usdg",  "S2_ORCA_USX_USDG",  9, 1.0),
]
_RAYDIUM = [
    ("raydium_usx_usdc",  "S2_RAYDIUM_USX_USDC",  9, 1.0),
    ("raydium_eusx_usx",  "S2_RAYDIUM_EUSX_USX",  4, 1.0),
]
_LOOPSCALE = [
    ("loopscale_supply_usx",  "S2_LOOPSCALE_SUPPLY_USX_ONE",  5, 1.0),
    ("loopscale_borrow_usx",  "S2_LOOPSCALE_BORROW_USX",      1, 1.0),
]


def _days_in_s2(now_ts: int) -> float:
    end = min(now_ts, S2_END_TS)
    return max(0.0, (end - S2_START_TS) / 86400.0)


class _PartnerExtractor(QuestExtractor):
    """Shared base for partner extractors that already cache through rpc_helper."""
    EXTRACTOR_FN = None
    QUEST_TABLE = []

    def cache_key(self) -> str: return self.SHARED_CACHE_KEY

    def extract(self, wallet: str) -> dict:
        positions = self.EXTRACTOR_FN(wallet) or {}
        return {
            "positions": positions,
            "_watermark": {"slot": 0, "ts": int(time.time())},
        }

    def transform(self, raw: dict, now_ts: int) -> dict:
        positions = raw.get("positions") or {}
        days = _days_in_s2(now_ts)
        out = {}
        for field, quest, mult, usd in self.QUEST_TABLE:
            amount = float(positions.get(field, 0) or 0)
            out[quest] = amount * usd * mult * days
        return out


class KaminoExtractor(_PartnerExtractor):
    QUEST_CODE = tuple(q for _, q, _, _ in _KAMINO)
    SHARED_CACHE_KEY = "S2_KAMINO"
    EXTRACTOR_FN = staticmethod(get_kamino_positions)
    QUEST_TABLE = _KAMINO


class OrcaExtractor(_PartnerExtractor):
    QUEST_CODE = tuple(q for _, q, _, _ in _ORCA)
    SHARED_CACHE_KEY = "S2_ORCA"
    EXTRACTOR_FN = staticmethod(get_orca_lp_positions)
    QUEST_TABLE = _ORCA


class RaydiumExtractor(_PartnerExtractor):
    QUEST_CODE = tuple(q for _, q, _, _ in _RAYDIUM)
    SHARED_CACHE_KEY = "S2_RAYDIUM"
    EXTRACTOR_FN = staticmethod(get_raydium_lp_positions)
    QUEST_TABLE = _RAYDIUM


class LoopscaleExtractor(_PartnerExtractor):
    QUEST_CODE = tuple(q for _, q, _, _ in _LOOPSCALE)
    SHARED_CACHE_KEY = "S2_LOOPSCALE"
    EXTRACTOR_FN = staticmethod(get_loopscale_positions)
    QUEST_TABLE = _LOOPSCALE
