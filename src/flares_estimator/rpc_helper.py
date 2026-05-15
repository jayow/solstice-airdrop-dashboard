"""
Shared RPC helper with automatic fallback to Solana public RPC when Helius quota
is exhausted. Used by all extractors to keep working past the daily Helius limit.
"""
import os, time, requests
from threading import Lock

_PROVIDER_PREFIXES = ("helius", "quicknode", "chainstack", "alchemy", "triton", "rpcpool")

def _read_endpoints_from_env():
    """Read all RPC URLs from .env. Helius keys are returned separately (used for
    enhanced /v0/transactions API) — others are appended to the rotation."""
    helius, extra = [], []
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
        for line in open(env_path):
            line = line.strip()
            if line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k_low, v = k.strip().lower(), v.strip()
            if not v: continue
            if k_low.startswith("helius") or k_low == "helius_api_key":
                helius.append(v if v.startswith("http") else f"https://mainnet.helius-rpc.com/?api-key={v}")
            elif any(k_low.startswith(p) for p in _PROVIDER_PREFIXES) and v.startswith("http"):
                extra.append(v)
    except Exception: pass
    return helius, extra

HELIUS_ENDPOINTS, EXTRA_ENDPOINTS = _read_endpoints_from_env()
if not HELIUS_ENDPOINTS:
    # No Helius key in .env → fall back to a public free Solana RPC.
    # All walkers/extractors will work but Helius DAS + Enhanced API features
    # won't be available, and quotas on the public endpoint are tight.
    HELIUS_ENDPOINTS = ["https://api.mainnet-beta.solana.com"]
HELIUS = os.environ.get("HELIUS_URL") or HELIUS_ENDPOINTS[0]
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
ANKR_RPC = "https://rpc.ankr.com/solana"
TRITON_FREE = "https://free.rpcpool.com"

# Rotation order: Helius first (fastest, cheapest credits) → other paid free-tiers
# (QuickNode/Chainstack/Alchemy) → free public/Triton/Ankr fallbacks.
ENDPOINTS = HELIUS_ENDPOINTS + EXTRA_ENDPOINTS + [TRITON_FREE, PUBLIC_RPC, ANKR_RPC]

_current_idx = 0
_lock = Lock()

# Endpoints that returned a quota error this session — skipped at start of each
# call so we don't burn a request per call on an already-exhausted provider.
# Reset on process restart (when daily quota presumably refreshes).
_quota_dead: set = set()

# Quota error codes
QUOTA_ERRORS = (-32429, -32413)  # Helius / generic over-limit
HTTP_RETRY_STATUS = (429, 503, 504)

# Idempotent read methods — eligible for the persistent disk cache. Mutating
# or volatile methods (getSlot, getBlockHeight, sendTransaction, etc) are not
# cached. Methods not in this set bypass the cache and always call live RPC.
CACHEABLE_METHODS = {
    "getAccountInfo",
    "getTokenAccountsByOwner",
    "getTokenAccountsByDelegate",
    "getProgramAccounts",
    "getMultipleAccounts",
    "getTransaction",
    "getSignaturesForAddress",
    "getTokenLargestAccounts",
    "getTokenSupply",
    "getInflationReward",
    "getBlockTime",
    # Helius-exclusive — single call replaces getSignaturesForAddress +
    # N × getTransaction. See helius_get_transactions_for_address() helper.
    "getTransactionsForAddress",
}

# Methods whose responses are IMMUTABLE for finalized data — once fetched,
# they will never change. Cache forever. Avoids re-burning credits on identical
# queries day after day (62k+ getTransaction entries × no-op refreshes).
IMMUTABLE_METHODS = {
    "getTransaction",  # finalized tx body never changes
    "getBlockTime",    # finalized slot's timestamp never changes
}

# Default cache freshness for MUTABLE methods: 24h. Long enough that re-running
# transforms in a session is free, short enough that day-to-day balance/sig
# data stays fresh.
DEFAULT_CACHE_MAX_AGE_HOURS = 24
# Effectively infinite (~11.4 years) — used for IMMUTABLE_METHODS.
INFINITE_CACHE_MAX_AGE_HOURS = 100_000


def _first_live_idx() -> int:
    """Return the lowest endpoint index not marked quota-dead this session."""
    for i in range(len(ENDPOINTS)):
        if i not in _quota_dead:
            return i
    return 0  # all dead — reset and try again


def rpc(method: str, params: list, timeout: int = 30, max_retries: int = 8,
        force_refresh: bool = False, cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS) -> dict:
    """Call RPC method with auto endpoint rotation, retry, AND persistent disk cache.

    Cache behavior:
      - If `method` is in CACHEABLE_METHODS and a fresh entry exists, return it
        immediately (no RPC call).
      - Otherwise call live RPC; on success, write result to cache.
      - `force_refresh=True` bypasses cache lookup but still writes on success.
      - Quota / network errors do NOT invalidate cache — caller can fall back to
        prior cached value via cache.get() if it wants.

    The cache is keyed by (method, params), persisted to data/rpc_cache/.
    See rpc_cache.py for storage format.
    """
    if method in CACHEABLE_METHODS and not force_refresh:
        # Immutable methods (getTransaction of finalized sigs, getBlockTime)
        # never need refresh — use infinite age regardless of caller's setting.
        effective_max_age = (INFINITE_CACHE_MAX_AGE_HOURS
                             if method in IMMUTABLE_METHODS
                             else cache_max_age_hours)
        try:
            from rpc_cache import get as _cache_get
            entry = _cache_get(method, params, max_age_hours=effective_max_age)
            if entry and entry.get("status") == "ok":
                return {"jsonrpc": "2.0", "id": 1, "result": entry["result"]}
        except Exception:
            pass  # cache failure should never break the RPC path
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    idx = _first_live_idx()

    for attempt in range(max_retries):
        endpoint = ENDPOINTS[idx % len(ENDPOINTS)]
        try:
            r = requests.post(endpoint, json=body, timeout=timeout)
            try: j = r.json()
            except Exception: j = {}
            err = j.get("error")
            if isinstance(err, str): err = {"message": err}
            err = err or {}
            err_code = err.get("code") if isinstance(err, dict) else None

            is_quota = (err_code in QUOTA_ERRORS)
            if is_quota or r.status_code == 429:
                _quota_dead.add(idx)
                if len(_quota_dead) >= len(ENDPOINTS):
                    _quota_dead.clear()
                    time.sleep(min(4, 0.5 * (2 ** attempt)))
                idx = _first_live_idx()
                continue

            if r.status_code in (503, 504):
                time.sleep(min(4, 0.3 * (2 ** attempt))); continue
            if r.status_code >= 400 and not j: return {}
            if err and err_code not in QUOTA_ERRORS: return {}
            if not err:
                # Cache successful read responses for future re-runs
                if method in CACHEABLE_METHODS and "result" in j:
                    try:
                        from rpc_cache import put as _cache_put
                        _cache_put(method, params, j["result"], status="ok")
                    except Exception:
                        pass
                return j
        except requests.exceptions.RequestException:
            time.sleep(min(4, 0.3 * (2 ** attempt)))

    return {}


def post_helius_batch(sigs: list, timeout: int = 45, max_retries: int = 6) -> list:
    """Helius enhanced-API tx batch. Falls through to per-sig getTransaction via rpc()
    if Helius is exhausted."""
    if not sigs: return []
    api_url = HELIUS.replace("mainnet.helius-rpc.com/?api-key=",
                              "api.helius.xyz/v0/transactions?api-key=")
    for attempt in range(max_retries):
        try:
            r = requests.post(api_url, json={"transactions": sigs}, timeout=timeout)
            if r.status_code in HTTP_RETRY_STATUS:
                time.sleep(min(4, 0.4 * (2 ** attempt))); continue
            j = r.json()
            if isinstance(j, dict) and j.get("error"):
                # Helius enhanced exhausted — fall back to per-sig parsed-tx fetch
                break
            if isinstance(j, list): return j
        except requests.exceptions.RequestException:
            time.sleep(min(4, 0.4 * (2 ** attempt)))

    # Fallback: per-sig getTransaction via rpc()
    out = []
    for sig in sigs:
        r = rpc("getTransaction", [sig, {"encoding":"jsonParsed", "maxSupportedTransactionVersion": 0}])
        tx = r.get("result")
        if tx:
            # Reshape to look like Helius enhanced-API format
            transfers = []
            for item in (tx.get("meta",{}).get("postTokenBalances", []) or []):
                # Skip — we'd need diff vs preTokenBalances; simpler approach: extract from instructions
                pass
            out.append({
                "signature": sig,
                "timestamp": tx.get("blockTime"),
                "tokenTransfers": _derive_token_transfers(tx),
                "instructions": tx.get("transaction",{}).get("message",{}).get("instructions",[]),
                "logMessages": tx.get("meta",{}).get("logMessages") or [],
            })
        else:
            out.append({"signature": sig})
    return out


def _derive_token_transfers(tx: dict) -> list:
    """Reconstruct token transfers from pre/post token balances diff."""
    pre = tx.get("meta",{}).get("preTokenBalances", []) or []
    post = tx.get("meta",{}).get("postTokenBalances", []) or []
    msg = tx.get("transaction",{}).get("message", {})
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys", [])]
    pre_by_idx = {p["accountIndex"]: p for p in pre}
    post_by_idx = {p["accountIndex"]: p for p in post}
    transfers = []
    for idx in set(pre_by_idx) | set(post_by_idx):
        a = pre_by_idx.get(idx, {})
        b = post_by_idx.get(idx, {})
        mint = (a.get("mint") or b.get("mint"))
        owner = (a.get("owner") or b.get("owner"))
        pre_amt = float((a.get("uiTokenAmount", {}) or {}).get("uiAmount") or 0)
        post_amt = float((b.get("uiTokenAmount", {}) or {}).get("uiAmount") or 0)
        delta = post_amt - pre_amt
        if abs(delta) < 1e-9: continue
        ata = keys[idx] if idx < len(keys) else None
        transfers.append({
            "mint": mint, "tokenAmount": abs(delta),
            "fromUserAccount": owner if delta < 0 else None,
            "toUserAccount":   owner if delta > 0 else None,
            "fromTokenAccount": ata if delta < 0 else None,
            "toTokenAccount":   ata if delta > 0 else None,
        })
    return transfers


def helius_get_transactions_for_address(address: str, transactionDetails: str = "full",
                                         sortOrder: str = "asc",
                                         min_block_time: int | None = None,
                                         max_block_time: int | None = None,
                                         status: str = "succeeded",
                                         max_per_page: int = 1000,
                                         max_pages: int = 200) -> list:
    """Paginate Helius's getTransactionsForAddress and return concatenated results.

    Single call replaces (getSignaturesForAddress + N × getTransaction) — Helius
    bundles tx body, meta, and balance data per page. Up to 1000 txs per page;
    follow `paginationToken` until null.

    Filters applied server-side:
      - blockTime range [min_block_time, max_block_time] (inclusive)
      - status (succeeded / failed / any)

    Caching: each PAGE is cached separately keyed by (address, page_token+filter
    fingerprint). Pagination tokens stay stable for finalized history so cache
    hits compound across runs.

    Returns: list of transaction objects (signatures mode) or full tx objects
    (full mode). Order: oldest-first when sortOrder='asc', newest-first 'desc'.

    Args:
      address: base58 pubkey to query
      transactionDetails: 'full' (default) or 'signatures'
      sortOrder: 'asc' (chronological) or 'desc' (newest first)
      min_block_time / max_block_time: inclusive blockTime bounds (unix seconds)
      status: 'succeeded' (default), 'failed', or 'any'
      max_per_page: 1..1000 (Helius cap)
      max_pages: defensive cap to avoid unbounded paging

    Raises if Helius is not the active provider.
    """
    if "helius" not in HELIUS.lower():
        raise RuntimeError("getTransactionsForAddress requires a Helius RPC endpoint")
    filters = {"status": status}
    if min_block_time is not None or max_block_time is not None:
        bt = {}
        if min_block_time is not None: bt["gte"] = min_block_time
        if max_block_time is not None: bt["lte"] = max_block_time
        filters["blockTime"] = bt

    all_txs = []
    pagination_token = None
    for _page in range(max_pages):
        params = [address, {
            "transactionDetails": transactionDetails,
            "sortOrder": sortOrder,
            "limit": max_per_page,
            "filters": filters,
        }]
        if pagination_token:
            params[1]["paginationToken"] = pagination_token
        r = rpc("getTransactionsForAddress", params, timeout=45)
        result = r.get("result") or {}
        page = result.get("data") or []
        if not page: break
        all_txs.extend(page)
        pagination_token = result.get("paginationToken")
        if not pagination_token: break
    return all_txs


if __name__ == "__main__":
    # Quick smoke test
    r = rpc("getSignaturesForAddress",
             ["5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN", {"limit": 3}])
    print("RPC works:", len(r.get("result", [])), "sigs returned")
    print("Active endpoint:", ENDPOINTS[_current_idx % len(ENDPOINTS)][:60])
