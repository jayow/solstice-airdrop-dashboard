"""
Orca Whirlpool LP position extractor for Flares estimation.

For each wallet:
  1. List all SPL token holdings with amount=1, decimals=0 (potential position NFTs)
  2. For each candidate NFT mint, derive Position PDA = ["position", mint] under WHIRL_PROG
  3. Fetch position account; decode whirlpool, liquidity, tick_lower, tick_upper
  4. If whirlpool matches a Solstice S2 pool, compute USD value via CLMM math using the
     pool's current sqrtPrice + token prices (token A in USD, token B in USD)

Uses Helius RPC + Orca v2 public API (https://api.orca.so/v2/solana/pools/{addr}).
"""
import os, struct, requests, base64
from typing import Dict, Optional
from solders.pubkey import Pubkey
from rpc_helper import rpc

HELIUS = os.environ.get("HELIUS_URL", "https://api.mainnet-beta.solana.com")
ORCA_API = "https://api.orca.so/v2/solana"

WHIRL_PROG = Pubkey.from_string("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc")

# Solstice S2-incentivized pools
S2_POOLS = {
    "2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix": "orca_usx_usdc",
    "AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf": "orca_eusx_usx",
    "45bdcbekD687TU49RFux1a4csf3TN3cM3J1UaFcFhWt2": "orca_usx_usdg",
}

_pool_data_cache: Dict[str, dict] = {}


def _get_pool(pool_addr: str) -> Optional[dict]:
    if pool_addr in _pool_data_cache:
        return _pool_data_cache[pool_addr]
    try:
        r = requests.get(f"{ORCA_API}/pools/{pool_addr}", timeout=15).json()
        d = r.get("data") or {}
        _pool_data_cache[pool_addr] = d
        return d
    except Exception:
        return None


def _wallet_nfts(wallet: str) -> list:
    """Return list of NFT mint addresses (amount=1, decimals=0) held by wallet."""
    out = []
    for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
        r = rpc("getTokenAccountsByOwner",
                [wallet, {"programId": prog}, {"encoding":"jsonParsed"}])
        for acc in r.get("result", {}).get("value", []) or []:
            data = acc.get("account", {}).get("data")
            # When jsonParsed succeeds, data is {"parsed":{"info":...}}.
            # When it fails (non-SPL account), data is [base64, "base64"] — skip.
            if not isinstance(data, dict): continue
            info = (data.get("parsed") or {}).get("info") or {}
            amt = info.get("tokenAmount", {}) or {}
            if amt.get("decimals") == 0 and int(amt.get("amount") or 0) >= 1:
                if info.get("mint"): out.append(info["mint"])
    return out


def _position_pda(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"position", bytes(Pubkey.from_string(mint))], WHIRL_PROG)
    return str(pda)


def _decode_position(data: bytes) -> Optional[dict]:
    """Decode Orca Whirlpool Position account.

    Layout (216 bytes total):
      0..8    discriminator
      8..40   whirlpool (32)
      40..72  position_mint (32)
      72..88  liquidity (u128)
      88..92  tick_lower_index (i32)
      92..96  tick_upper_index (i32)
      ...
    """
    if len(data) < 96: return None
    from solders.pubkey import Pubkey as PK
    whirl = str(PK(bytes(data[8:40])))
    liquidity = int.from_bytes(data[72:88], "little")
    tick_lower = struct.unpack("<i", data[88:92])[0]
    tick_upper = struct.unpack("<i", data[92:96])[0]
    return {"whirlpool": whirl, "liquidity": liquidity,
            "tick_lower": tick_lower, "tick_upper": tick_upper}


def _liquidity_to_amounts(liquidity: int, sqrt_price_q64: int,
                           tick_lower: int, tick_upper: int) -> tuple:
    """Given concentrated liquidity, current sqrtPrice (Q64.64), and tick range,
    return (amount_a, amount_b) in raw token units.

    sqrt_price_at_tick = 1.0001^(tick/2) → Q64.64 form: int(sqrt(1.0001^tick) * 2^64)
    """
    Q64 = 1 << 64
    def sqrt_price_at_tick(t: int) -> int:
        # Use float for tick bounds (precision is fine for USD conversion)
        return int((1.0001 ** (t / 2.0)) * Q64)

    sp_lower = sqrt_price_at_tick(tick_lower)
    sp_upper = sqrt_price_at_tick(tick_upper)
    sp = sqrt_price_q64

    if sp <= sp_lower:
        amount_a = liquidity * (sp_upper - sp_lower) * Q64 // (sp_lower * sp_upper)
        amount_b = 0
    elif sp >= sp_upper:
        amount_a = 0
        amount_b = liquidity * (sp_upper - sp_lower) // Q64
    else:
        amount_a = liquidity * (sp_upper - sp) * Q64 // (sp * sp_upper)
        amount_b = liquidity * (sp - sp_lower) // Q64
    return amount_a, amount_b


def _position_usd(pool: dict, position: dict) -> float:
    sp = int(pool["sqrtPrice"])
    a, b = _liquidity_to_amounts(position["liquidity"], sp,
                                  position["tick_lower"], position["tick_upper"])
    decA = pool["tokenA"]["decimals"]
    decB = pool["tokenB"]["decimals"]
    # Token prices: derive from pool price and known stable. For USX/USDC, USX/USDG, eUSX/USX
    # we treat all as ≈$1 (eUSX×1.03). Use pool['price'] as A→B ratio.
    sym_a = pool["tokenA"]["symbol"].upper()
    sym_b = pool["tokenB"]["symbol"].upper()
    def usd(sym: str) -> float:
        if sym == "EUSX": return 1.03
        return 1.0  # USX, USDC, USDG ≈ $1
    return (a / 10**decA) * usd(sym_a) + (b / 10**decB) * usd(sym_b)


def get_orca_lp_positions(wallet: str) -> Dict[str, float]:
    """Returns USD value of LP positions per Solstice S2 Orca pool."""
    out = {"orca_usx_usdc": 0.0, "orca_eusx_usx": 0.0, "orca_usx_usdg": 0.0}

    nfts = _wallet_nfts(wallet)
    if not nfts:
        return out

    # Derive position PDAs for all NFTs
    pdas = []
    for mint in nfts:
        try:
            pdas.append((mint, _position_pda(mint)))
        except Exception:
            continue

    # Batch-fetch account info
    pda_addrs = [p[1] for p in pdas]
    if not pda_addrs:
        return out

    # getMultipleAccounts in chunks of 100
    accounts = []
    for i in range(0, len(pda_addrs), 100):
        chunk = pda_addrs[i:i+100]
        r = rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
        accounts.extend(r.get("result", {}).get("value", []) or [])

    for (mint, pda), acc in zip(pdas, accounts):
        if not acc: continue
        if acc.get("owner") != str(WHIRL_PROG): continue
        data = base64.b64decode(acc["data"][0])
        pos = _decode_position(data)
        if not pos: continue
        if pos["whirlpool"] not in S2_POOLS: continue
        pool_data = _get_pool(pos["whirlpool"])
        if not pool_data: continue
        usd = _position_usd(pool_data, pos)
        key = S2_POOLS[pos["whirlpool"]]
        out[key] += usd

    return out


if __name__ == "__main__":
    import sys, json
    wallet = sys.argv[1] if len(sys.argv) > 1 else "5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN"
    print(json.dumps(get_orca_lp_positions(wallet), indent=2))
