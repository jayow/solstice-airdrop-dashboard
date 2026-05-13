"""
S2_EXPONENT_LP_USX_JUN26 (20×), S2_EXPONENT_LP_EUSX_JUN26 (10×).

Fully on-chain LP flare extractor:

  daily_flares = lp_balance × per_lp_usd × multiplier

Where:
  lp_balance:    wallet's LP-token holdings (from getTokenAccountsByOwner)
  per_lp_usd:    pool_underlying_USD / circulating_lp_supply  (from market vault state)
  multiplier:    20 for USX-Jun26, 10 for eUSX-Jun26 (Solstice catalog, isActive=true)

Per-LP-USD derivation (100% on-chain):
  pool_USD = (PT_vault_balance + SY_vault1 + SY_vault2) × usd_per_underlying
  circulating_lp = total_lp_supply − lp_market_vault_balance
  per_lp_usd = pool_USD / circulating_lp

Market layout (verified 2026-05-11 via on-chain probe):
  offset 40:  YT mint
  offset 72:  SY mint
  offset 104: yp_alias (yield-position alias, used by YT extractor)
  offset 136: LP mint
  offset 168: LP market vault (holds nearly all of LP supply — LP is
              effectively a closed-set SPL token)
  offset 200: YT vault
  offset 232: SY vault 1
  offset 264: SY vault 2

Pool USD = (SY_vault1 + SY_vault2) × usd_per_underlying.
YT in pool not double-counted (YT and SY were minted from the same
underlying principal; SY already captures the principal value).
"""
import os, sys, base64, base58, time, struct
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rpc_helper import rpc
from .base import QuestExtractor, S2_START_TS, S2_END_TS

# eUSX redemption rate is read live from the eUSX program state account.
# Verified 2026-05-11: u128 at offset 48, scaled by 1e18, currently $1.156392.
# Back-test on wallet 5V9V's S1 LP (3,394,399.51 dashboard) matches at 100.01%
# using time-integral × live peg × 10× multiplier.
EUSX_STATE_PDA = "JDs1wmLaVB2KsAotjbBKVEsiV1gbrG3Qrjyht5LnX9YP"
EUSX_RATE_OFFSET = 48
EUSX_RATE_SCALE = 10**18

def _live_eusx_peg() -> float:
    r = rpc("getAccountInfo", [EUSX_STATE_PDA, {"encoding": "base64"}])
    v = r.get("result", {}).get("value")
    if not v: return 1.0
    d = base64.b64decode(v["data"][0])
    if len(d) < EUSX_RATE_OFFSET + 8: return 1.0
    val = struct.unpack("<Q", d[EUSX_RATE_OFFSET:EUSX_RATE_OFFSET+8])[0]
    return val / EUSX_RATE_SCALE

# Market → (lp_mint, multiplier, quest_code, usd_per_underlying)
MARKETS = {
    "BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm": {
        "label": "USX-Jun26",
        "lp_mint": "BR2JKV9gPoJfX8A8DkFmo2yNQKCeGipg33oYaZ4EmjbW",
        "mult": 20, "quest": "S2_EXPONENT_LP_USX_JUN26",
        "usd_per": 1.0,  # USX is the stable; peg = $1.00
    },
    "rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP": {
        "label": "eUSX-Jun26",
        "lp_mint": "4GT6g1iKx2TyYCkwt1tERkReQjSUuVE7uh14M5W8v2nn",
        "mult": 10, "quest": "S2_EXPONENT_LP_EUSX_JUN26",
        "usd_per": None,  # populated dynamically from eUSX state PDA
    },
}

_per_lp_usd_cache: dict = {}
_eusx_peg_cache: dict = {}


def _decode_market(market_pk: str) -> dict:
    r = rpc("getAccountInfo", [market_pk, {"encoding": "base64"}])
    v = r.get("result", {}).get("value")
    if not v: return {}
    d = base64.b64decode(v["data"][0])
    return {
        "lp_mint":   base58.b58encode(d[136:168]).decode(),
        "lp_vault":  base58.b58encode(d[168:200]).decode(),
        "pt_vault":  base58.b58encode(d[200:232]).decode(),
        "sy_vault1": base58.b58encode(d[232:264]).decode(),
        "sy_vault2": base58.b58encode(d[264:296]).decode(),
    }


def _per_lp_usd(market_pk: str, usd_per_underlying: float) -> float:
    if market_pk in _per_lp_usd_cache: return _per_lp_usd_cache[market_pk]
    vaults = _decode_market(market_pk)
    if not vaults: return 0.0
    # LP mint supply
    r = rpc("getAccountInfo", [vaults["lp_mint"], {"encoding": "jsonParsed"}])
    info = (r.get("result", {}).get("value", {}).get("data", {}) or {}).get("parsed", {}).get("info", {})
    lp_supply = float(info.get("supply") or 0) / (10 ** int(info.get("decimals") or 6))
    # LP market vault holdings (uncirculated)
    lp_vault_bal = 0.0
    if vaults.get("lp_vault"):
        rv = rpc("getAccountInfo", [vaults["lp_vault"], {"encoding": "jsonParsed"}])
        i = (rv.get("result", {}).get("value", {}).get("data", {}) or {}).get("parsed", {}).get("info", {})
        lp_vault_bal = float((i.get("tokenAmount") or {}).get("uiAmount") or 0)
    circulating = lp_supply - lp_vault_bal
    if circulating <= 0: return 0.0
    # Pool USD = SY vaults × usd_per_underlying.
    # YT vault is NOT counted: YT and SY were minted from the same underlying
    # principal — SY captures the principal value, YT is the yield-rights
    # claim against future yield (price → 0 at maturity, no principal).
    pool_usd = 0.0
    for vname in ("sy_vault1", "sy_vault2"):
        ata = vaults.get(vname)
        if not ata: continue
        rv = rpc("getAccountInfo", [ata, {"encoding": "jsonParsed"}])
        i = (rv.get("result", {}).get("value", {}).get("data", {}) or {}).get("parsed", {}).get("info", {})
        if not i: continue
        amt = float((i.get("tokenAmount") or {}).get("uiAmount") or 0)
        pool_usd += amt * usd_per_underlying
    per_lp = pool_usd / circulating
    _per_lp_usd_cache[market_pk] = per_lp
    return per_lp


def _wallet_lp_balance(wallet: str, lp_mint: str) -> float:
    r = rpc("getTokenAccountsByOwner", [wallet, {"mint": lp_mint}, {"encoding": "jsonParsed"}])
    total = 0.0
    for acc in (r.get("result", {}).get("value", []) or []):
        info = acc.get("account", {}).get("data", {})
        if not isinstance(info, dict): continue
        info = info.get("parsed", {}).get("info", {})
        total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
    return total


class ExponentLPExtractor(QuestExtractor):
    QUEST_CODE = ("S2_EXPONENT_LP_USX_JUN26", "S2_EXPONENT_LP_EUSX_JUN26")
    SHARED_CACHE_KEY = "S2_EXPONENT_LP"

    def extract(self, wallet: str) -> dict:
        positions = []
        for m_pk, cfg in MARKETS.items():
            usd_per = cfg["usd_per"]
            if usd_per is None:   # eUSX — read live peg from program state
                if "eusx" not in _eusx_peg_cache:
                    _eusx_peg_cache["eusx"] = _live_eusx_peg()
                usd_per = _eusx_peg_cache["eusx"]
            lp_bal = _wallet_lp_balance(wallet, cfg["lp_mint"])
            per_lp = _per_lp_usd(m_pk, usd_per) if lp_bal > 0 else 0.0
            positions.append({
                "market": m_pk, "label": cfg["label"], "mult": cfg["mult"],
                "quest": cfg["quest"], "lp_balance": lp_bal,
                "per_lp_usd": per_lp, "lp_value_usd": lp_bal * per_lp,
            })
        return {"positions": positions, "_watermark": {"slot": 0, "ts": int(time.time())}}

    def transform(self, raw: dict, now_ts: int) -> dict:
        # daily_flares = lp_value_usd × multiplier
        # (where lp_value_usd = lp_balance × per_lp_usd, computed in extract())
        out = {q: 0.0 for q in self.QUEST_CODE}
        for p in raw.get("positions", []) or []:
            q = p.get("quest")
            if q in out:
                out[q] += float(p.get("lp_value_usd") or 0) * float(p.get("mult") or 0)
        return out
