"""
Persistent RPC response cache.

Why this exists: every bulk run before this re-extracted everything via RPC,
wasting quota on data that hadn't changed AND silently zeroing out valid rows
when RPC errored. With a cache:

  1. Re-running ANY transform/load step is FREE (no RPC).
  2. Iterating on logic (rate constants, classifier rules) doesn't burn RPC.
  3. Errors stay errors — we never overwrite a known-good cached value with
     a fresh RPC failure unless explicitly told to refresh.

Storage: data/rpc_cache/<method>/<param_hash>.json
Each entry:
  { "method": "...", "params": [...], "result": {...},
    "status": "ok"|"empty"|"error", "fetched_at": "2026-05-10T..." }

Lookup: cached_rpc(method, params, max_age_hours=None) returns:
  - cached result if found and fresh
  - falls through to live rpc() and stores result on success
  - re-raises on RPC error (caller decides how to handle)

Bypass cache: pass force_refresh=True to skip lookup.
"""
import os, json, hashlib, time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(ROOT, "data", "rpc_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_locks: dict = {}
_locks_master = Lock()


def _key(method: str, params: list) -> str:
    """Stable hash of (method, params)."""
    payload = json.dumps([method, params], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _path(method: str, params: list) -> str:
    h = _key(method, params)
    d = os.path.join(CACHE_DIR, method)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{h}.json")


def _lock_for(path: str) -> Lock:
    with _locks_master:
        if path not in _locks:
            _locks[path] = Lock()
        return _locks[path]


def get(method: str, params: list, max_age_hours: Optional[float] = None) -> Optional[dict]:
    """Return cached entry if present (and fresh, if max_age_hours given). Else None."""
    p = _path(method, params)
    if not os.path.exists(p): return None
    try:
        with open(p) as f: entry = json.load(f)
    except Exception:
        return None
    if max_age_hours is not None:
        try:
            fetched = datetime.fromisoformat(entry["fetched_at"].replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
            if age_h > max_age_hours: return None
        except Exception:
            return None
    return entry


def put(method: str, params: list, result: Any, status: str = "ok") -> None:
    """Atomic write of an RPC response to cache."""
    p = _path(method, params)
    entry = {
        "method": method,
        "params": params,
        "result": result,
        "status": status,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = p + ".tmp"
    with _lock_for(p):
        with open(tmp, "w") as f: json.dump(entry, f)
        os.replace(tmp, p)


def cached_rpc(method: str, params: list, *, force_refresh: bool = False,
               max_age_hours: Optional[float] = 24) -> dict:
    """RPC call that consults disk cache first.

    Returns the SAME shape as raw rpc() (i.e. the JSON-RPC response dict with
    'result'/'error'). On cache hit, returns the cached result wrapped to match.
    On miss (or force_refresh), calls live rpc() and stores result on success.
    """
    if not force_refresh:
        entry = get(method, params, max_age_hours=max_age_hours)
        if entry and entry.get("status") == "ok":
            return {"jsonrpc": "2.0", "id": 1, "result": entry["result"]}

    # Live call, lazy import to avoid circular at module load
    from rpc_helper import rpc as live_rpc
    j = live_rpc(method, params)
    if j and "result" in j and "error" not in j:
        put(method, params, j["result"], status="ok")
    elif j and j.get("error"):
        # Don't cache errors — keep prior good entry usable. Returning the
        # response lets caller decide.
        pass
    return j


def stats() -> dict:
    """Walk the cache dir and report sizes per method."""
    out = {}
    if not os.path.isdir(CACHE_DIR): return out
    for method in sorted(os.listdir(CACHE_DIR)):
        d = os.path.join(CACHE_DIR, method)
        if not os.path.isdir(d): continue
        files = [f for f in os.listdir(d) if f.endswith(".json")]
        size = sum(os.path.getsize(os.path.join(d, f)) for f in files)
        out[method] = {"entries": len(files), "size_kb": round(size / 1024, 1)}
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))
