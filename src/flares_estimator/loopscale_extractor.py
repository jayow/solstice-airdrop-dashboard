"""
Loopscale supply/borrow extractor for Flares estimation.

Uses Loopscale's public API at https://tars.loopscale.com/v1.

Quests covered:
  S2_LOOPSCALE_SUPPLY_USX_ONE: USD value of user's deposit in the USX ONE vault (LP-token denominated)
  S2_LOOPSCALE_BORROW_USX:     sum of USD principal across all active USX-principal loans for borrower
"""
import os, requests
from typing import Dict

LOOP_API = "https://tars.loopscale.com/v1"
HELIUS = os.environ.get("HELIUS_URL", "https://api.mainnet-beta.solana.com")

USX_MINT = "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"
EUSX_MINT = "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC"

# USX ONE Vault (S2-incentivized). Resolved via /markets/lending_vaults/info.
USX_ONE_VAULT = "3s3vAaYpwkyjrgzpBRwgSDxpwHPD1jic25mb1VDzM8Rk"
USX_ONE_LP_MINT = "3PQotuGMnMgEXrErizQbzPPhSMb79xQgkEDn2hk2KPWn"

# USX RWA Vault — Lend USX against Real World Assets. Same mechanic as USX ONE,
# different vault address and LP share mint. Quest S2_LOOPSCALE_SUPPLY_USX_RWA
# at multiplier 5×. Vault NOT featured on Solstice's /api/partners cards yet,
# but the quest is active in /api/flares/quests and ~$2M is deposited.
USX_RWA_VAULT = "FLu6RJEr4bKAfd37oqCdZE6zoiyUZQELN7WYBN3VR3hP"
USX_RWA_LP_MINT = "9nHYLLXsigUSv8bMDdwXNQKowAPRrqgU4JQNuiSwJqrx"

# Cached vault state
_vault_cache: Dict[str, dict] = {}


def _rpc(method: str, params: list, timeout: int = 30) -> dict:
    from rpc_helper import rpc as _shared_rpc
    return _shared_rpc(method, params, timeout=timeout)


def _get_vault(vault_addr: str) -> Dict:
    if vault_addr in _vault_cache:
        return _vault_cache[vault_addr]
    try:
        r = requests.post(
            f"{LOOP_API}/markets/lending_vaults/info",
            json={"vaultAddresses":[vault_addr], "page":0, "pageSize":1},
            timeout=15
        ).json()
        items = r.get("lendVaults", [])
        if items:
            _vault_cache[vault_addr] = items[0]
            return items[0]
    except Exception:
        pass
    return {}


def _get_lp_balance(wallet: str, lp_mint: str) -> float:
    """Get raw LP token balance (uiAmount) held by wallet."""
    r = _rpc("getTokenAccountsByOwner",
             [wallet, {"mint": lp_mint}, {"encoding":"jsonParsed"}])
    total = 0.0
    for acc in r.get("result", {}).get("value", []) or []:
        info = acc["account"]["data"]["parsed"]["info"]
        total += float(info.get("tokenAmount",{}).get("uiAmount") or 0)
    return total


def get_loopscale_positions(wallet: str) -> Dict[str, float]:
    out = {
        "loopscale_supply_usx": 0.0,
        "loopscale_borrow_usx": 0.0,
    }

    # 1. USX ONE vault deposit
    lp_amt = _get_lp_balance(wallet, USX_ONE_LP_MINT)
    if lp_amt > 0:
        v = _get_vault(USX_ONE_VAULT)
        if v:
            vault = v["vault"]
            # share value = cumulativePrincipalDeposited / lpSupply (USX 6 decimals; LP same)
            try:
                lp_supply = float(vault["lpSupply"]) / 1e6
                cum_dep = float(vault["cumulativePrincipalDeposited"]) / 1e6
            except Exception:
                lp_supply = cum_dep = 0
            if lp_supply > 0:
                share_value = cum_dep / lp_supply
                # USX ≈ $1
                out["loopscale_supply_usx"] = lp_amt * share_value

    # 2. USX active borrows (filterType=0 = Active only). For closed-during-S2 loans we'd need
    #    to walk historical sigs; the API doesn't expose closed-loans by-borrower.
    try:
        r = requests.post(
            f"{LOOP_API}/markets/loans/info",
            json={
                "borrowers":[wallet],
                "principalMints":[USX_MINT],
                "filterType": 0,  # active
                "page": 0, "pageSize": 100
            },
            timeout=15
        ).json()
        for loan in r.get("items", []):
            usd = float(loan.get("principalUsd", 0) or 0)
            out["loopscale_borrow_usx"] += usd
    except Exception:
        pass

    return out


def get_loopscale_borrow_history(wallet: str) -> list:
    """Walk all Loopscale loans (any status) for a wallet — for time-weighted accuracy.
    Returns list of {start_ts, end_ts, principal_usd}. end_ts is now if loan is active.
    """
    history = []
    page = 0
    while True:
        try:
            r = requests.post(
                f"{LOOP_API}/markets/loans/info",
                json={
                    "borrowers":[wallet],
                    "principalMints":[USX_MINT],
                    "page": page, "pageSize": 100  # no filterType → all statuses
                },
                timeout=15
            ).json()
        except Exception:
            break
        items = r.get("items", []) or []
        if not items: break
        for loan in items:
            l = loan.get("loan", {})
            ledgers = loan.get("ledgers", []) or []
            for ld in ledgers:
                if ld.get("principalMint") != USX_MINT: continue
                start = int(ld.get("startTime") or l.get("startTime") or 0)
                end = int(ld.get("endTime") or 0) or None
                # Use principalDue / 1e6 (USX = 6 decimals) as principal at start;
                # note: this is the loan-level principal; for accurate time-weighting you'd
                # walk sub-events. Best-effort: principal × duration.
                principal_raw = float(ld.get("principalDue") or 0)
                principal_usx = principal_raw / 1e6  # 6 decimals
                history.append({
                    "start_ts": start,
                    "end_ts": end,
                    "principal_usx": principal_usx,
                    "loan": l.get("address"),
                    "is_active": l.get("loanStatus") == 0,
                    "closed": bool(l.get("closed")),
                })
        if len(items) < 100: break
        page += 1
    return history


if __name__ == "__main__":
    import sys, json
    wallet = sys.argv[1] if len(sys.argv) > 1 else "5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN"
    print(json.dumps(get_loopscale_positions(wallet), indent=2))
