"""
Forward-refresh Kamino caches: walk reserve-account sigs from latest cached
through current chain tip, fetch + parse each new tx, append to kamino_events.jsonl.

Closes the biggest remaining accuracy gap: Kamino events post-2026-04-16.

Run: python3 refresh_kamino.py
"""
import os, sys, json, re, time, threading
import requests
from concurrent.futures import ThreadPoolExecutor

# Allow imports from this dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kamino_markets import RESERVES, MINT_TO_RESERVE, KAMINO_LEND_PROGRAM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGS_PATH   = os.path.join(ROOT, "data/kamino_sigs.json")
EVENTS_PATH = os.path.join(ROOT, "data/kamino_events.jsonl")

ENV = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1)
    ENV[k] = v
RAW_KEY = ENV.get("HELIUS_API_KEY", "")
KEY = re.search(r"api-key=([^&]+)", RAW_KEY).group(1) if RAW_KEY.startswith("http") else RAW_KEY
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
HELIUS_API = f"https://api.helius.xyz/v0/transactions?api-key={KEY}"


def rpc_call(method, params, retries=10):
    body = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for i in range(retries):
        try:
            r = requests.post(RPC, json=body, timeout=30)
            if r.status_code in (429, 413, 503, 504):
                time.sleep(min(4, 0.3*(2**i))); continue
            j = r.json()
            if j.get("error"):
                code = (j["error"].get("code") or 0)
                msg = j["error"].get("message", "").lower()
                if code in (-32429, -32413) or "too many" in msg or "max usage" in msg:
                    time.sleep(min(4, 0.3*(2**i))); continue
                raise RuntimeError(j["error"])
            return j.get("result")
        except requests.exceptions.RequestException:
            time.sleep(min(4, 0.3*(2**i)))
    raise RuntimeError("rpc retries exhausted")


def walk_forward(address, since_sig):
    """Get all sigs newer than `since_sig` for `address`. Helius supports `until` param."""
    new_sigs = []
    before = None
    while True:
        params = [address, {"limit": 1000, "until": since_sig}]
        if before: params[1]["before"] = before
        page = rpc_call("getSignaturesForAddress", params)
        if not page: break
        new_sigs.extend(page)
        if len(page) < 1000: break
        before = page[-1]["signature"]
    return new_sigs


def load_existing_events_sigs():
    if not os.path.exists(EVENTS_PATH): return set()
    seen = set()
    for line in open(EVENTS_PATH):
        try: seen.add(json.loads(line).get("sig"))
        except Exception: continue
    return seen


def fetch_helius_batch(sigs, retries=10):
    for i in range(retries):
        try:
            r = requests.post(HELIUS_API,
                              json={"transactions": sigs},
                              headers={"Content-Type":"application/json"}, timeout=45)
            if r.status_code in (429, 413, 503, 504):
                time.sleep(min(8, 0.5*(2**i))); continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            time.sleep(min(8, 0.5*(2**i)))
    return []


def classify_actions_from_logs(tx):
    """Map Kamino instruction names found in logs to user-facing action.
    Returns dict: signer → [list of action names found in this tx]."""
    actions = []
    for log in tx.get("logMessages", []) or []:
        # Kamino logs include "Instruction: <name>"
        m = re.search(r"Instruction:\s+(\w+)", log)
        if not m: continue
        name = m.group(1)
        # Map to action class
        action = None
        if "DepositReserveLiquidity" in name: action = "supply"
        elif "WithdrawReserveLiquidity" in name or "WithdrawObligationCollateral" in name: action = "withdraw"
        elif "Borrow" in name and "Flash" not in name: action = "borrow"
        elif "FlashBorrow" in name: action = "flashBorrow"
        elif "RepayObligation" in name or "Repay" in name: action = "repay"
        if action: actions.append((name, action))
    return actions


def parse_events_from_tx(tx):
    """Emit per-(signer, mint) events from a Helius enhanced tx."""
    if not tx or tx.get("transactionError"): return []
    instrs = tx.get("instructions") or []
    if not any(i.get("programId") == KAMINO_LEND_PROGRAM for i in instrs): return []

    transfers = tx.get("tokenTransfers") or []
    if not transfers: return []

    fee_payer = tx.get("feePayer")
    actions = classify_actions_from_logs(tx)
    primary_action = None
    primary_instr = None
    if actions:
        # Most recent (top-level) action wins
        primary_instr, primary_action = actions[0]

    events = []
    by_signer_mint = {}  # (signer, mint) -> delta
    for tr in transfers:
        mint = tr.get("mint")
        if mint not in MINT_TO_RESERVE: continue
        amt = float(tr.get("tokenAmount") or 0)
        # Track delta from feePayer's perspective
        if tr.get("toUserAccount") == fee_payer:   by_signer_mint[(fee_payer, mint)] = by_signer_mint.get((fee_payer, mint), 0) + amt
        if tr.get("fromUserAccount") == fee_payer: by_signer_mint[(fee_payer, mint)] = by_signer_mint.get((fee_payer, mint), 0) - amt

    for (signer, mint), delta in by_signer_mint.items():
        if abs(delta) < 1e-9: continue
        sym, r = MINT_TO_RESERVE[mint]
        action = primary_action
        if not action:
            action = "inflow" if delta > 0 else "outflow"
        events.append({
            "sig": tx.get("signature"),
            "blockTime": tx.get("timestamp"),
            "reserve": sym,
            "mint": mint,
            "signer": signer,
            "action": action,
            "underlyingDelta": round(delta, 6),
            "usdNet": round(abs(delta) * r["px"], 4),
            "instr": primary_instr or "?",
        })
    return events


def main():
    # 1. Find latest cached sig per reserve
    print("Loading existing sigs cache...", flush=True)
    if not os.path.exists(SIGS_PATH):
        print(f"ERROR: {SIGS_PATH} missing"); return
    existing = json.load(open(SIGS_PATH))
    print(f"  {len(existing):,} existing sigs (latest blockTime: {max(s.get('blockTime') or 0 for s in existing)})")

    # Most-recent sig signature is the boundary
    by_block = sorted(existing, key=lambda s: -(s.get("blockTime") or 0))
    if not by_block:
        print("Empty sigs cache, aborting"); return
    latest_sig = by_block[0]["signature"]
    print(f"  latest sig: {latest_sig}")

    # 2. For each reserve, walk forward
    new_sigs = {}
    for sym, r in RESERVES.items():
        addr = r["reserve"]
        print(f"\n[{sym}] walking forward from {latest_sig[:10]}...", flush=True)
        try:
            page = walk_forward(addr, latest_sig)
        except Exception as e:
            print(f"  ERROR: {e}"); continue
        added = 0
        for s in page:
            sig = s.get("signature")
            if not sig or s.get("err"): continue
            if sig not in new_sigs:
                new_sigs[sig] = {"signature": sig, "blockTime": s.get("blockTime")}
                added += 1
        print(f"  +{added} new sigs", flush=True)

    if not new_sigs:
        print("\nNo new sigs to fetch. Cache is already current.")
        return

    print(f"\nFetched {len(new_sigs):,} new sig metadata. Now parsing transactions...")

    # 3. Drop sigs already in events file
    existing_event_sigs = load_existing_events_sigs()
    todo = [s for s in new_sigs.keys() if s not in existing_event_sigs]
    print(f"  {len(todo):,} sigs need parsing (rest already in events file)")

    # 4. Fetch + parse in batches of 100, write events
    written = 0
    with open(EVENTS_PATH, "a") as out_fh:
        for i in range(0, len(todo), 100):
            batch = todo[i:i+100]
            txs = fetch_helius_batch(batch)
            for tx in (txs or []):
                evs = parse_events_from_tx(tx)
                if evs:
                    for ev in evs:
                        out_fh.write(json.dumps(ev) + "\n")
                        written += 1
                else:
                    # Still record sig to mark as processed (avoid re-fetching)
                    out_fh.write(json.dumps({"sig": tx.get("signature"),
                                              "blockTime": tx.get("timestamp"),
                                              "events": []}) + "\n")
            done = i + len(batch)
            print(f"  parsed {done}/{len(todo):,}  events written: {written}", flush=True)

    # 5. Append to sigs cache
    print(f"\nUpdating {SIGS_PATH}...")
    by_key = {s["signature"]: s for s in existing}
    for sig, rec in new_sigs.items():
        by_key.setdefault(sig, rec)
    out_arr = sorted(by_key.values(), key=lambda s: -(s.get("blockTime") or 0))
    json.dump(out_arr, open(SIGS_PATH, "w"))
    print(f"Wrote {len(out_arr):,} sigs ({len(out_arr)-len(existing):,} new)")
    print(f"Total new events written: {written}")


if __name__ == "__main__":
    main()
