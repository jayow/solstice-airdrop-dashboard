"""
Convert quest_results.jsonl (per-quest flares per wallet) → flares_stage3.{jsonl,csv}
in the legacy format expected by filter_pdas.py / classify_offline.py / build_data.py.

This is the bridge between the new ELT framework and the existing dashboard pipeline.
"""
import os, sys, json, csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quest_map import QUESTS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA = os.path.join(ROOT, "data")

# Build {quest_code: protocol} from the canonical quest map
QUEST_PROTO = {q["code"]: q["protocol"] for q in QUESTS}

PROTOCOL_COLUMNS = ["solstice", "yield_vault", "exponent", "kamino", "whirlpool", "raydium", "loopscale"]


def main():
    src = os.path.join(DATA, "quest_results.jsonl")
    out_jsonl = os.path.join(DATA, "flares_stage3.jsonl")
    out_csv = os.path.join(DATA, "flares_stage3.csv")

    rows = []
    with open(src) as f:
        for line in f:
            try: r = json.loads(line)
            except: continue
            wallet = r.get("wallet")
            flares = r.get("flares") or {}
            cols = {p: 0.0 for p in PROTOCOL_COLUMNS}
            for q, v in flares.items():
                proto = QUEST_PROTO.get(q)
                if proto and proto in cols:
                    cols[proto] += float(v or 0)
            total = sum(cols.values())
            rows.append({
                "wallet": wallet,
                **{k: round(v, 2) for k, v in cols.items()},
                "total": round(total, 2),
                "current_tvl_usd": 0,  # legacy field; not currently used by classifier
                "tier": "default",
                "_breakdown": [{"quest": q, "flares": round(v, 2)} for q, v in flares.items() if v > 0],
            })

    # Atomic writes
    tmp_j = out_jsonl + ".new"
    with open(tmp_j, "w") as f:
        for r in sorted(rows, key=lambda x: x["wallet"]):
            f.write(json.dumps(r) + "\n")
    os.replace(tmp_j, out_jsonl)

    fields = ["wallet"] + PROTOCOL_COLUMNS + ["total", "current_tvl_usd", "tier"]
    tmp_c = out_csv + ".new"
    with open(tmp_c, "w") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["wallet"]):
            w.writerow(r)
    os.replace(tmp_c, out_csv)

    n_with_flares = sum(1 for r in rows if r["total"] > 0)
    total_flares = sum(r["total"] for r in rows)
    print(f"Wrote {len(rows):,} stage3 rows ({n_with_flares:,} with positive flares)")
    print(f"Total flares: {total_flares:,.0f}")
    print(f"  → {out_jsonl}")
    print(f"  → {out_csv}")


if __name__ == "__main__":
    main()
