"""
Kamino positions extractor for Flares estimation.

Uses Kamino's public REST API (https://api.kamino.finance) to pull:
  - Obligations (lend/borrow) on the Solstice Market (9Y7uw...) — covers
    USX/eUSX/USDG/USDC reserves
  - kVault positions across USDG strategy vaults

Outputs USD-denominated TVL per Flares quest. Since USX, USDG, and eUSX (×1.03)
all peg to USD, USD value ≈ token amount for the quest formulas.

NOTE on caching: Kamino's API is REST/HTTP-GET, not Solana JSON-RPC, so we
cannot route through rpc_helper.rpc() (which POSTs JSON-RPC bodies to Solana
endpoints). Instead we hit the same persistent disk cache (rpc_cache) directly,
which is what rpc_helper.rpc() uses internally. Result: cache coverage is
identical to RPC reads — repeat lookups for the same wallet are free.
"""
import requests
from typing import Dict, Optional

from rpc_cache import get as _cache_get, put as _cache_put

KAMINO_API = "https://api.kamino.finance"
SOLSTICE_MARKET = "9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU"
PRIMARY_MARKET = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"

USX_MINT  = "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"
EUSX_MINT = "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC"
USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"

SF_DENOM = 2**60  # Kamino "Fraction" scale (Q-format)

# Cache TTL for Kamino REST responses. 24h matches rpc_helper's default.
_KAMINO_CACHE_HOURS = 24

# Cached reserve→mint maps per market (filled lazily)
_reserve_to_mint_cache: Dict[str, Dict[str, str]] = {}
_kvault_cache: Optional[Dict[str, str]] = None  # vault_pubkey → tokenMint
_kvault_share_price_cache: Dict[str, float] = {}  # vault_pubkey → share price USD


def _kamino_get(path: str, timeout: int = 20):
    """GET https://api.kamino.finance{path}, with persistent disk cache.

    Same caching layer rpc_helper.rpc() uses (rpc_cache module). Method key is
    namespaced so it never collides with real Solana JSON-RPC methods.
    """
    method = "kamino_REST_GET"
    params = [path]
    entry = _cache_get(method, params, max_age_hours=_KAMINO_CACHE_HOURS)
    if entry and entry.get("status") == "ok":
        return entry["result"]
    r = requests.get(f"{KAMINO_API}{path}", timeout=timeout).json()
    try:
        _cache_put(method, params, r, status="ok")
    except Exception:
        pass
    return r


def _get_reserve_to_mint(market: str) -> Dict[str, str]:
    if market in _reserve_to_mint_cache:
        return _reserve_to_mint_cache[market]
    r = _kamino_get(f"/kamino-market/{market}/reserves/metrics", timeout=20)
    mapping = {item["reserve"]: item["liquidityTokenMint"] for item in r if item.get("reserve")}
    _reserve_to_mint_cache[market] = mapping
    return mapping


def _get_relevant_kvaults() -> Dict[str, str]:
    """Return {vault_pubkey: tokenMint} for vaults relevant to S2 (USDG, USX-pair strategies)."""
    global _kvault_cache
    if _kvault_cache is not None:
        return _kvault_cache
    r = _kamino_get("/kvaults/vaults", timeout=20)
    relevant = {}
    for v in r:
        st = v.get("state", {})
        mint = st.get("tokenMint", "")
        # S2 quest is "KVAULT_USDG_USX" — vaults denominated in USDG that lend to USX reserves
        if mint == USDG_MINT:
            relevant[v["address"]] = mint
    _kvault_cache = relevant
    return relevant


def _sf_to_usd(sf_str: str) -> float:
    try:
        return int(sf_str) / SF_DENOM
    except (ValueError, TypeError):
        return 0.0


def get_kamino_positions(wallet: str) -> Dict[str, float]:
    """
    Returns USD-denominated TVL per Kamino S2 quest.

    Keys:
      kamino_supply_usx, kamino_supply_eusx, kamino_supply_usdg
      kamino_borrow_usx, kamino_borrow_usdg
      kamino_kvault_usx_usdg
    """
    out = {
        "kamino_supply_usx": 0.0,
        "kamino_supply_eusx": 0.0,
        "kamino_supply_usdg": 0.0,
        "kamino_borrow_usx": 0.0,
        "kamino_borrow_usdg": 0.0,
        "kamino_kvault_usx_usdg": 0.0,
    }

    # 1. Obligations on Solstice Market (USX/eUSX/USDG)
    sol_reserves = _get_reserve_to_mint(SOLSTICE_MARKET)
    obls = _kamino_get(
        f"/kamino-market/{SOLSTICE_MARKET}/users/{wallet}/obligations",
        timeout=20
    ) or []

    for obl in obls:
        state = obl.get("state", {})
        for dep in state.get("deposits", []):
            reserve = dep.get("depositReserve", "")
            if reserve == "11111111111111111111111111111111": continue
            mint = sol_reserves.get(reserve)
            if not mint: continue
            usd = _sf_to_usd(dep.get("marketValueSf", "0"))
            if usd == 0: continue
            if mint == USX_MINT:  out["kamino_supply_usx"]  += usd
            elif mint == EUSX_MINT: out["kamino_supply_eusx"] += usd
            elif mint == USDG_MINT: out["kamino_supply_usdg"] += usd

        for bor in state.get("borrows", []):
            reserve = bor.get("borrowReserve", "")
            if reserve == "11111111111111111111111111111111": continue
            mint = sol_reserves.get(reserve)
            if not mint: continue
            usd = _sf_to_usd(bor.get("marketValueSf", "0"))
            if usd == 0: continue
            if mint == USX_MINT:  out["kamino_borrow_usx"]  += usd
            elif mint == USDG_MINT: out["kamino_borrow_usdg"] += usd

    # 2. kVault positions (USDG vaults that route to USX reserves)
    positions = _kamino_get(
        f"/kvaults/users/{wallet}/positions",
        timeout=20
    ) or []

    relevant_vaults = _get_relevant_kvaults()
    for pos in positions:
        vault = pos.get("vaultAddress") or pos.get("vault") or pos.get("vaultPubkey")
        if vault not in relevant_vaults: continue
        try:
            shares = float(pos.get("totalShares", "0"))
        except (ValueError, TypeError):
            shares = 0.0
        if shares == 0: continue
        share_price = _get_kvault_share_price(vault)
        out["kamino_kvault_usx_usdg"] += shares * share_price

    return out


def _get_kvault_share_price(vault: str) -> float:
    if vault in _kvault_share_price_cache:
        return _kvault_share_price_cache[vault]
    try:
        r = _kamino_get(f"/kvaults/vaults/{vault}/metrics", timeout=15)
        sp = float(r.get("sharePrice", 1.0))
    except Exception:
        sp = 1.0
    _kvault_share_price_cache[vault] = sp
    return sp


if __name__ == "__main__":
    import sys, json
    wallet = sys.argv[1] if len(sys.argv) > 1 else "E7TiAhE81qYQf9BkVvDeDnPnJyhFREqpGfEJNXsnfWwu"
    print(json.dumps(get_kamino_positions(wallet), indent=2))
