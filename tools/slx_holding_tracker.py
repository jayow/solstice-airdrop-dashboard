"""Track post-claim SLX activity per wallet.

For each wallet that has claimed: snapshot current SLX + stSLX balance and
categorize via categorize() into one of (priority order):
  STAKED      — holds meaningful stSLX (>10 SLX or >5% of liquid claim)
  BOUGHT_MORE — total balance > 110% of liquid claimed
  HELD        — total balance >= 99% of liquid claimed
  SOLD        — total balance <  99% of liquid claimed
  UNCLAIMED   — liquid claim <= 0.001 (filtered out at the SELECT, never reached)

Uses Helius RPC getTokenAccountsByOwner (no fragile ATA derivation).
Runs forever; snapshots every 15 min into slx_balance_snapshots table.

Silent-zero guard (added 2026-06-13 after the 2026-06-03 corruption that
flipped 13,639 wallets to SOLD): a wallet that was ever non-zero cannot
become 0+0 in a single 15-min cycle without a real sell tx. Treat as RPC
malfunction and skip rather than overwrite the prior healthy row.
"""
import sqlite3, time, json, os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')
RPC = 'https://mainnet.helius-rpc.com/?api-key=e225afce-b56f-4494-9138-1e9c48c5c425'
SLX_MINT = 'SLXdx4BUt2v9uJQNzWqSfzTJ9UKLUDsvxHFMEEdrfgq'         # Token classic
STSLX_MINT = 'GxHksENo754dKj6kv5d2z7ey9KwE7YSRYgRCtoFYd2yq'     # Staked Solstice via GLAM (Token-2022, real prod mint)
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022    = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
CYCLE_SEC = 900   # 15 min between snapshots
WORKERS = 8       # concurrent RPC

def init_db(con):
    con.execute("""CREATE TABLE IF NOT EXISTS slx_balance_snapshots (
        wallet TEXT NOT NULL,
        ts INTEGER NOT NULL,
        balance_slx REAL,
        balance_stslx REAL,
        liquid_claimed REAL,
        delta_pct REAL,
        category TEXT,
        PRIMARY KEY (wallet, ts)
    )""")
    # Schema migration: add balance_stslx if missing
    try:
        con.execute("ALTER TABLE slx_balance_snapshots ADD COLUMN balance_stslx REAL")
    except Exception:
        pass  # already exists
    con.execute("CREATE INDEX IF NOT EXISTS ix_balance_wallet ON slx_balance_snapshots(wallet, ts DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_balance_ts ON slx_balance_snapshots(ts DESC)")
    con.commit()

def _fetch_mint_balance(sess, wallet, mint, program_id):
    """One RPC: balance of `mint` (under a specific token program) for `wallet`.

    Returns None on RPC failure or malformed body (a missing "result"/"value"
    means the call did not actually return data — must not be conflated with a
    legitimate zero balance). Returns 0.0 only when the call succeeded and the
    wallet genuinely owns no tokens of that mint."""
    try:
        r = sess.post(RPC, json={"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
            "params":[wallet, {"programId": program_id}, {"encoding":"jsonParsed"}]}, timeout=10)
        body = r.json()
        # Distinguish "RPC error / malformed reply" from "wallet genuinely has 0".
        # Without this, both cases collapse to 0 and silently flip everyone to SOLD
        # (cf. the 2026-06-03 corruption that wiped 13,639 wallets' balances).
        if "result" not in body or not isinstance(body["result"], dict) or "value" not in body["result"]:
            return None
        accs = body["result"]["value"]
        bal = 0.0
        for a in accs:
            info = a['account']['data']['parsed']['info']
            if info.get('mint') != mint: continue
            try: bal += float(info['tokenAmount']['uiAmountString'] or 0)
            except Exception: pass
        return bal
    except Exception:
        return None

def fetch_balance(wallet, sess):
    """Returns (wallet, slx_bal, stslx_bal, err).

    err is non-None whenever the snapshot MUST NOT be written, so the caller
    keeps the prior healthy row in slx_balance_snapshots rather than
    overwriting it with a false zero."""
    slx = _fetch_mint_balance(sess, wallet, SLX_MINT, TOKEN_PROGRAM)
    stslx = _fetch_mint_balance(sess, wallet, STSLX_MINT, TOKEN_2022)
    if slx is None and stslx is None:
        return (wallet, None, None, "both calls failed")
    # One-leg failure: skip rather than write a partial row that would mis-categorize
    # (a SLX-success-stSLX-fail wallet would look SOLD if it had only stSLX).
    if slx is None or stslx is None:
        return (wallet, None, None, "one RPC leg failed")
    return (wallet, slx or 0.0, stslx or 0.0, None)

def categorize(slx_bal, stslx_bal, liquid):
    """Categorize wallet behavior post-claim. Priority: STAKED > BOUGHT_MORE > HELD > SOLD.

    STAKED takes precedence as soon as a wallet holds meaningful stSLX, since the act
    of locking SLX in the staking vault is a deliberate commitment regardless of whether
    they also sold some of their position.
    """
    if liquid <= 0.001: return "UNCLAIMED"
    total = (slx_bal or 0) + (stslx_bal or 0)   # ~1:1 peg approximation
    pct = total / liquid * 100
    # Threshold for STAKED: any stSLX worth > 10 SLX OR > 5% of liquid claim
    if (stslx_bal or 0) > 10 or ((stslx_bal or 0) / max(liquid, 0.001) > 0.05):
        return "STAKED"
    if pct > 110:           return "BOUGHT_MORE"
    if pct >= 99:           return "HELD"
    return "SOLD"

def run_cycle(con):
    cur = con.cursor()
    cur.execute("""SELECT wallet, liquid_slx FROM slx_allocations
                   WHERE batch != '_NONE_' AND claim_at != '' AND liquid_slx > 0.001""")
    targets = cur.fetchall()
    if not targets:
        print(f"  no claimed wallets yet", flush=True); return

    # Seed the "previously non-zero" guard. If a wallet had balance > 0 in any
    # prior snapshot but RPC now returns (0,0), refuse to overwrite and skip —
    # this prevents the 2026-06-03 silent-zero corruption from recurring.
    cur.execute("""SELECT wallet, MAX(balance_slx + COALESCE(balance_stslx, 0))
                   FROM slx_balance_snapshots GROUP BY wallet""")
    prior_nonzero = {w for w, mx in cur.fetchall() if (mx or 0) > 0.001}

    now = int(time.time())
    sess = requests.Session()
    inserts = []
    skipped_silent_zero = 0
    cats = {"HELD":0,"SOLD":0,"BOUGHT_MORE":0,"STAKED":0,"UNCLAIMED":0}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_balance, w, sess): (w, liq) for w, liq in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            wallet, slx_bal, stslx_bal, err = fut.result()
            _, liquid = futs[fut]
            if err:
                continue
            # Defensive: a wallet that was ever non-zero cannot legitimately
            # become 0/0 in one 15-min cycle without an on-chain sell. Treat as
            # a likely silent RPC malfunction and skip.
            if (slx_bal or 0) == 0.0 and (stslx_bal or 0) == 0.0 and wallet in prior_nonzero:
                skipped_silent_zero += 1
                continue
            cat = categorize(slx_bal, stslx_bal, liquid)
            cats[cat] = cats.get(cat,0) + 1
            total = (slx_bal or 0) + (stslx_bal or 0)
            delta_pct = (total / liquid * 100) if liquid > 0 else 0
            inserts.append((wallet, now, slx_bal or 0, stslx_bal or 0, liquid, delta_pct, cat))

    cur.executemany("""INSERT OR REPLACE INTO slx_balance_snapshots
        (wallet, ts, balance_slx, balance_stslx, liquid_claimed, delta_pct, category)
        VALUES (?,?,?,?,?,?,?)""", inserts)
    con.commit()
    dt = time.time() - t0
    ts_str = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))
    print(f"[{ts_str}] snapshot: {len(inserts)} wallets in {dt:.0f}s · "
          f"SOLD={cats['SOLD']} HELD={cats['HELD']} STAKED={cats['STAKED']} BOUGHT_MORE={cats['BOUGHT_MORE']} "
          f"skipped_silent_zero={skipped_silent_zero}",
          flush=True)

    # Surface most interesting changes since previous snapshot
    cur.execute("""SELECT s.wallet, s.balance_slx, s.balance_stslx, s.liquid_claimed, s.delta_pct, s.category
                   FROM slx_balance_snapshots s
                   WHERE s.ts = ? AND s.liquid_claimed > 100 AND s.category IN ('SOLD','BOUGHT_MORE','STAKED')
                   ORDER BY ABS(s.balance_slx + s.balance_stslx - s.liquid_claimed) DESC LIMIT 10""", (now,))
    notable = cur.fetchall()
    if notable:
        print("  TOP movements (this snapshot):", flush=True)
        for w, slx, stslx, liq, pct, cat in notable:
            print(f"    {cat:11s} {w[:14]}…  liq={liq:>12,.2f}  slx={slx:>12,.2f} stSLX={stslx:>10,.2f} ({pct:>6.1f}%)", flush=True)

def main():
    # Schema init once at startup; per-cycle connections open/close so the
    # WAL reader-mark is released across the 15-min sleep (otherwise the WAL
    # cannot be checkpointed and grows monotonically — was 10 GB).
    con0 = sqlite3.connect(DB)
    init_db(con0)
    con0.close()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] holding tracker started (cycle={CYCLE_SEC}s)", flush=True)
    while True:
        con = sqlite3.connect(DB)
        try:
            run_cycle(con)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  cycle err: {e}", flush=True)
        finally:
            con.close()
        time.sleep(CYCLE_SEC)

if __name__ == "__main__":
    main()
