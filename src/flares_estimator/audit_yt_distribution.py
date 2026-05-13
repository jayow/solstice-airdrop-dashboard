"""Audit: enumerate full distribution of active YT positions across the 2 Solstice
YT markets and reconcile against on-chain YT mint supply.

Output: data/yt_distribution_audit.json + per-market summary.
"""
import os, sys, json, base64, base58, struct, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc

EXPO = "ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7"

MARKETS = {
    "BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm": {"label": "USX-Jun26", "mult": 30},
    "rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP": {"label": "eUSX-Jun26", "mult": 15},
}


def main():
    print("[1/4] resolving market metadata", flush=True)
    yt_mint_per_market = {}
    yp_alias_per_market = {}
    for m_pk, cfg in MARKETS.items():
        r = rpc("getAccountInfo", [m_pk, {"encoding": "base64"}])
        d = base64.b64decode(r["result"]["value"]["data"][0])
        yt_mint = base58.b58encode(d[40:72]).decode()
        yp_alias = base58.b58encode(d[104:136]).decode()
        yt_mint_per_market[m_pk] = yt_mint
        yp_alias_per_market[yp_alias] = (m_pk, cfg)
        rs = rpc("getAccountInfo", [yt_mint, {"encoding": "jsonParsed"}])
        info = rs.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {})
        supply = float(info.get("supply") or 0) / (10 ** int(info.get("decimals") or 6))
        cfg["yt_mint"] = yt_mint
        cfg["yp_alias"] = yp_alias
        cfg["yt_supply"] = supply
        print(f"  {cfg['label']}: yt_mint={yt_mint[:8]}…  yp_alias={yp_alias[:8]}…  supply={supply:,.2f}", flush=True)

    print("\n[2/4] enumerating all e35c92 size=164 program accounts", flush=True)
    t0 = time.time()
    r = rpc("getProgramAccounts", [EXPO, {
        "encoding": "base64",
        "filters": [{"dataSize": 164}],
    }], timeout=180)
    accs = r.get("result", []) or []
    print(f"  total size=164 accounts: {len(accs):,}  ({time.time()-t0:.1f}s)", flush=True)

    print("\n[3/4] classifying by market and computing distribution", flush=True)
    per_market = {m_pk: {"holders": {}, "yt_sum": 0.0, "n_records": 0} for m_pk in MARKETS}
    n_e35c92 = 0
    for a in accs:
        try:
            d = base64.b64decode(a["account"]["data"][0])
        except Exception: continue
        if len(d) < 80: continue
        disc = d[:8].hex()
        if disc != "e35c92311d55475e": continue
        n_e35c92 += 1
        authority = base58.b58encode(d[8:40]).decode()
        yp_alias = base58.b58encode(d[40:72]).decode()
        yt_amount = struct.unpack("<Q", d[72:80])[0] / 1e6
        if yp_alias not in yp_alias_per_market: continue
        m_pk, cfg = yp_alias_per_market[yp_alias]
        bucket = per_market[m_pk]
        bucket["yt_sum"] += yt_amount
        bucket["n_records"] += 1
        if yt_amount > 0:
            bucket["holders"][authority] = bucket["holders"].get(authority, 0.0) + yt_amount

    print(f"  e35c92 disc records: {n_e35c92:,}  ({sum(b['n_records'] for b in per_market.values()):,} in Solstice markets)", flush=True)

    print("\n[4/4] per-market reconciliation:")
    for m_pk, cfg in MARKETS.items():
        b = per_market[m_pk]
        non_zero_wallets = len(b["holders"])
        print(f"\n  {cfg['label']} ({m_pk[:8]}…)")
        print(f"    YT mint supply (on-chain):    {cfg['yt_supply']:>20,.2f}")
        print(f"    Σ YT in e35c92 size-164 accs: {b['yt_sum']:>20,.2f}")
        print(f"    e35c92 record count:          {b['n_records']:>20,}")
        print(f"    Distinct holders (yt>0):      {non_zero_wallets:>20,}")
        diff = cfg["yt_supply"] - b["yt_sum"]
        pct = (diff / cfg["yt_supply"] * 100) if cfg["yt_supply"] else 0
        print(f"    Δ supply - sum:               {diff:>20,.2f}  ({pct:+.2f}%)")
        print(f"    Daily flares pool (yt×mult):  {b['yt_sum'] * cfg['mult']:>20,.2f}")

        # Top 10 holders
        top10 = sorted(b["holders"].items(), key=lambda x: -x[1])[:10]
        print(f"    Top 10 holders:")
        for w, yt in top10:
            print(f"      {w}  {yt:,.2f} YT  → {yt*cfg['mult']:,.2f} daily flares")

    # Save full distribution
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                              "data", "yt_distribution_audit.json")
    payload = {m_pk: {
        "label": cfg["label"],
        "mult": cfg["mult"],
        "yt_supply_onchain": cfg["yt_supply"],
        "yt_sum_holders": per_market[m_pk]["yt_sum"],
        "holders": per_market[m_pk]["holders"],
    } for m_pk, cfg in MARKETS.items()}
    with open(out_path, "w") as f: json.dump(payload, f)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
