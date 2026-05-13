"""
Raydium CLMM LP position extractor for Flares estimation.

Raydium CLMM PersonalPositionState layout (281 bytes):
  0..8    discriminator (466f967ee60f1975)
  8       bump (u8)
  9..41   nft_mint (Pubkey)
  41..73  pool_id (Pubkey)
  73..77  tick_lower_index (i32)
  77..81  tick_upper_index (i32)
  81..97  liquidity (u128)
  ...

Position PDA = ["position", nft_mint] under CAMMCzo... program.

We derive USD value the same way as Orca: liquidity + tick range + current sqrtPrice.
"""
import os, math, struct, requests, base64
from typing import Dict, Optional
from solders.pubkey import Pubkey
from rpc_helper import rpc

HELIUS = os.environ.get("HELIUS_URL", "https://api.mainnet-beta.solana.com")
RAYDIUM_API = "https://api-v3.raydium.io"
RAYDIUM_CLMM = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")

S2_POOLS = {
    "EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6": "raydium_usx_usdc",
    "BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n": "raydium_eusx_usx",
}

_pool_cache: Dict[str, dict] = {}


def _get_pool(pool_id: str) -> Optional[dict]:
    if pool_id in _pool_cache:
        return _pool_cache[pool_id]
    try:
        r = requests.get(f"{RAYDIUM_API}/pools/info/ids?ids={pool_id}", timeout=15).json()
        data = r.get("data") or []
        if data:
            _pool_cache[pool_id] = data[0]
            return data[0]
    except Exception:
        pass
    return None


def _wallet_nfts(wallet: str) -> list:
    out = []
    for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
        r = rpc("getTokenAccountsByOwner",
                [wallet, {"programId": prog}, {"encoding":"jsonParsed"}], timeout=30)
        for acc in r.get("result", {}).get("value", []) or []:
            data = acc.get("account", {}).get("data")
            if not isinstance(data, dict): continue  # un-parseable, skip
            info = (data.get("parsed") or {}).get("info") or {}
            amt = info.get("tokenAmount", {}) or {}
            if amt.get("decimals") == 0 and int(amt.get("amount") or 0) >= 1:
                if info.get("mint"): out.append(info["mint"])
    return out


def _position_pda(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"position", bytes(Pubkey.from_string(mint))], RAYDIUM_CLMM)
    return str(pda)


def _decode_position(data: bytes) -> Optional[dict]:
    if len(data) < 97: return None
    pool = str(Pubkey(bytes(data[41:73])))
    tick_lower = struct.unpack("<i", data[73:77])[0]
    tick_upper = struct.unpack("<i", data[77:81])[0]
    liquidity = int.from_bytes(data[81:97], "little")
    return {"pool": pool, "tick_lower": tick_lower, "tick_upper": tick_upper, "liquidity": liquidity}


def _liquidity_to_amounts(liquidity: int, sqrt_price_q64: int,
                           tick_lower: int, tick_upper: int) -> tuple:
    Q64 = 1 << 64
    def sp(t: int) -> int:
        return int((1.0001 ** (t / 2.0)) * Q64)
    sp_lower = sp(tick_lower)
    sp_upper = sp(tick_upper)
    s = sqrt_price_q64
    if s <= sp_lower:
        a = liquidity * (sp_upper - sp_lower) * Q64 // (sp_lower * sp_upper)
        b = 0
    elif s >= sp_upper:
        a = 0
        b = liquidity * (sp_upper - sp_lower) // Q64
    else:
        a = liquidity * (sp_upper - s) * Q64 // (s * sp_upper)
        b = liquidity * (s - sp_lower) // Q64
    return a, b


def _position_usd(pool: dict, position: dict) -> float:
    # price = mintB per mintA. sqrtPrice_x64 = sqrt(price * 10^(decB-decA)) * 2^64
    price = float(pool["price"])
    decA = pool["mintA"]["decimals"]
    decB = pool["mintB"]["decimals"]
    raw_price = price * (10 ** (decB - decA))
    sqrt_price_q64 = int(math.sqrt(raw_price) * (1 << 64))
    a, b = _liquidity_to_amounts(position["liquidity"], sqrt_price_q64,
                                  position["tick_lower"], position["tick_upper"])
    sym_a = pool["mintA"]["symbol"].upper()
    sym_b = pool["mintB"]["symbol"].upper()
    def usd(s: str) -> float:
        return 1.03 if s == "EUSX" else 1.0
    return (a / 10**decA) * usd(sym_a) + (b / 10**decB) * usd(sym_b)


def get_raydium_lp_positions(wallet: str) -> Dict[str, float]:
    out = {"raydium_usx_usdc": 0.0, "raydium_eusx_usx": 0.0}

    nfts = _wallet_nfts(wallet)
    if not nfts: return out

    pdas = []
    for mint in nfts:
        try:
            pdas.append((mint, _position_pda(mint)))
        except Exception:
            continue
    if not pdas: return out

    accounts = []
    for i in range(0, len(pdas), 100):
        chunk = [p[1] for p in pdas[i:i+100]]
        r = rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}], timeout=30)
        accounts.extend(r.get("result", {}).get("value", []) or [])

    for (mint, pda), acc in zip(pdas, accounts):
        if not acc: continue
        if acc.get("owner") != str(RAYDIUM_CLMM): continue
        data = base64.b64decode(acc["data"][0])
        pos = _decode_position(data)
        if not pos: continue
        if pos["pool"] not in S2_POOLS: continue
        pool_data = _get_pool(pos["pool"])
        if not pool_data: continue
        usd = _position_usd(pool_data, pos)
        out[S2_POOLS[pos["pool"]]] += usd

    return out


if __name__ == "__main__":
    import sys, json
    wallet = sys.argv[1] if len(sys.argv) > 1 else "5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN"
    print(json.dumps(get_raydium_lp_positions(wallet), indent=2))
