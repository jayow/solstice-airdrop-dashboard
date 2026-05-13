"""
Per-quest ELT base. Every quest module subclasses QuestExtractor and implements:

  extract(wallet)           — fetch raw on-chain data needed by this quest
  extract_incremental(...)  — fetch only new data since watermark
  transform(raw, now_ts)    — pure function: cached raw → flares for this quest

Cache layout (the "Load" stage):
  data/quest_cache/{quest_code}/{wallet}.json
    {
      "wallet": "...",
      "quest_code": "...",
      "raw": { ... per-quest schema ... },
      "watermark_slot": <int>,     # latest slot fully indexed; resume from here
      "watermark_ts":   <iso>,
      "extracted_at":   <iso>,
      "schema_version": 1
    }

Watermark file:
  data/quest_watermarks.json   # {wallet: {quest_code: {"slot": ..., "ts": ...}}}
  Lets the daily refresh job pull only new data per (wallet, quest).
"""
import os, sys, json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

# Lazy DB import — only initialized when used (so file-only environments still work)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import db as _db
except Exception:
    _db = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
QUEST_CACHE_DIR = os.path.join(ROOT, "data", "quest_cache")  # legacy file path (read-only fallback)
WATERMARKS_PATH = os.path.join(ROOT, "data", "quest_watermarks.json")
USE_DB = _db is not None
os.makedirs(QUEST_CACHE_DIR, exist_ok=True)

# Shared S2 window
S2_START_TS = 1776038400  # 2026-04-13 05:00 UTC
S2_END_TS   = 1785024000  # 2026-08-01 00:00 UTC

_watermarks_lock = Lock()
_watermarks_cache: Optional[dict] = None


def _load_watermarks() -> dict:
    global _watermarks_cache
    with _watermarks_lock:
        if _watermarks_cache is None:
            if os.path.exists(WATERMARKS_PATH):
                try:
                    _watermarks_cache = json.load(open(WATERMARKS_PATH))
                except Exception:
                    _watermarks_cache = {}
            else:
                _watermarks_cache = {}
        return _watermarks_cache


def _save_watermarks(data: dict):
    with _watermarks_lock:
        tmp = WATERMARKS_PATH + ".tmp"
        with open(tmp, "w") as f: json.dump(data, f)
        os.replace(tmp, WATERMARKS_PATH)


def get_watermark(wallet: str, quest_code: str) -> dict:
    """Return {slot: int, ts: int} for last fully-indexed point. Empty dict if none."""
    wm = _load_watermarks()
    return wm.get(wallet, {}).get(quest_code, {})


def set_watermark(wallet: str, quest_code: str, slot: int, ts: int) -> None:
    wm = _load_watermarks()
    if wallet not in wm: wm[wallet] = {}
    wm[wallet][quest_code] = {"slot": int(slot), "ts": int(ts)}
    _save_watermarks(wm)


def quest_cache_path(quest_code: str, wallet: str) -> str:
    d = os.path.join(QUEST_CACHE_DIR, quest_code)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{wallet}.json")


def load_quest_cache(quest_code: str, wallet: str) -> Optional[dict]:
    """Return cached raw entry for (wallet, quest). DB-first; falls back to legacy file."""
    if USE_DB:
        try:
            entry = _db.get_cache(wallet, quest_code)
            if entry: return entry
        except Exception: pass
    # legacy file fallback (read-only)
    p = quest_cache_path(quest_code, wallet)
    if not os.path.exists(p): return None
    try: return json.load(open(p))
    except Exception: return None


def save_quest_cache(quest_code: str, wallet: str, raw: dict, watermark_slot: int = 0,
                     watermark_ts: int = 0, schema_version: int = 1) -> None:
    """Atomic write of raw extracted data. Writes to DB if available; file otherwise."""
    if USE_DB:
        try:
            _db.put_cache(wallet, quest_code, raw,
                          watermark_slot=watermark_slot,
                          watermark_ts=watermark_ts)
            if watermark_slot or watermark_ts:
                set_watermark(wallet, quest_code, watermark_slot, watermark_ts)
            return
        except Exception as e:
            print(f'  WARN db write failed for {wallet[:8]}.. {quest_code}: {e} — falling back to file', flush=True)
    # legacy file write
    p = quest_cache_path(quest_code, wallet)
    entry = {
        "wallet": wallet,
        "quest_code": quest_code,
        "raw": raw,
        "watermark_slot": int(watermark_slot),
        "watermark_ts": int(watermark_ts),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema_version,
    }
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(entry, f)
    os.replace(tmp, p)
    if watermark_slot or watermark_ts:
        set_watermark(wallet, quest_code, watermark_slot, watermark_ts)


class QuestExtractor(ABC):
    """ELT base for one quest (or a group of related quests sharing raw data).

    Subclasses set:
      QUEST_CODE: str | tuple — the quest_code(s) this module owns.
                                A single string for a single quest, OR a tuple of
                                quest codes if multiple quests share raw data
                                (e.g. HOLD_USX_DAILY/1MO/3MO all walk USX ATA).
      MULTIPLIER: int | dict   — flares multiplier(s); dict if QUEST_CODE is tuple.
      SHARED_CACHE_KEY: str    — when multiple quests share raw data, the cache
                                  key used for storage (often the family code).
    """
    QUEST_CODE: str = ""
    MULTIPLIER = 0
    SHARED_CACHE_KEY: str = ""  # if blank, defaults to QUEST_CODE

    def cache_key(self) -> str:
        return self.SHARED_CACHE_KEY or (
            self.QUEST_CODE if isinstance(self.QUEST_CODE, str) else self.QUEST_CODE[0]
        )

    @abstractmethod
    def extract(self, wallet: str) -> dict:
        """Full historical extract from S2_START_TS forward.
        Returns the raw dict that gets stored in the cache."""
        ...

    def extract_incremental(self, wallet: str, since_slot: int) -> Optional[dict]:
        """Pull only new on-chain data since `since_slot`. Default no-op: subclasses
        that benefit from incremental refresh override this. Returns merged raw or None."""
        return None

    @abstractmethod
    def transform(self, raw: dict, now_ts: int) -> dict:
        """Pure function: cached raw → {quest_code: flares}.
        NO RPC. NO file I/O outside the cached input.
        Returns a dict because some modules own multiple quest codes."""
        ...

    def run(self, wallet: str, now_ts: int, force_refresh: bool = False) -> dict:
        """End-to-end: extract (if needed) → load → transform.
        Returns {quest_code: flares} for all quests this module owns."""
        cached = None
        if not force_refresh:
            cached = load_quest_cache(self.cache_key(), wallet)
        if cached is None:
            raw = self.extract(wallet)
            wm = raw.get("_watermark", {})
            save_quest_cache(self.cache_key(), wallet, raw,
                             watermark_slot=wm.get("slot", 0),
                             watermark_ts=wm.get("ts", 0))
        else:
            raw = cached["raw"]
        return self.transform(raw, now_ts)
