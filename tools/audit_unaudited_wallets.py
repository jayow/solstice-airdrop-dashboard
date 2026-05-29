"""Audit the wallets that are in `wallets` table but NOT yet in wallet_onchain_audit.

These are typically S1-only wallets with no current S2 quest activity (no rows in
quest_cache). We still want to know if they exist on-chain, whether they're EOA / PDA /
program, owner program, etc. — so the classification surface is complete across the full
universe.

Reuses the helpers from tools/audit_onchain_classification.py.
"""
import json
import os
import sqlite3
import sys
import time

# import sibling module
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audit_onchain_classification as base  # noqa: E402


def load_unaudited(conn):
    rows = conn.execute(
        """
        SELECT w.wallet
        FROM wallets w
        LEFT JOIN wallet_onchain_audit a ON a.wallet = w.wallet
        WHERE a.wallet IS NULL
        """
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def audit():
    conn = sqlite3.connect(base.DB_PATH)
    base.ensure_table(conn)
    universe = load_unaudited(conn)
    tvl_map = base.load_tvl_map()

    print(f"[audit-unaudited] {len(universe)} wallets to audit", flush=True)
    n_with_tvl = sum(1 for w in universe if tvl_map.get(w, 0) > 0)
    n_high_tvl = sum(1 for w in universe if tvl_map.get(w, 0) > 10000)
    print(f"[audit-unaudited] {n_with_tvl} with any TVL, {n_high_tvl} above $10k (extra fetch)", flush=True)

    audited_at = int(time.time())
    errors = []
    n_done = 0
    BATCH = 100

    for i in range(0, len(universe), BATCH):
        batch = universe[i:i + BATCH]
        j = base.rpc("getMultipleAccounts", [batch, {"encoding": "base64"}])
        if not isinstance(j, dict) or "result" not in j:
            errors.append((i, str(j)[:200]))
            print(f"[audit-unaudited] batch {i} failed: {str(j)[:200]}", flush=True)
            continue
        values = j["result"].get("value") if isinstance(j["result"], dict) else None
        if values is None:
            errors.append((i, "no value in result"))
            continue
        rows = []
        for wallet, acct in zip(batch, values):
            exists, kind, owner, lamports, _ = base.classify(acct)
            n_sigs = None
            n_tok = None
            if tvl_map.get(wallet, 0) > 10000:
                try:
                    n_sigs, n_tok = base.fetch_extra_for_high_tvl(wallet)
                except Exception as e:
                    errors.append((wallet, f"extra fetch: {e}"))
            rows.append((wallet, 1 if exists else 0, kind, owner, lamports, n_sigs, n_tok, audited_at))
        conn.executemany(
            """
            INSERT INTO wallet_onchain_audit
                (wallet, exists_onchain, kind, owner_program, lamports, n_sigs_seen, n_token_accts, audited_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                exists_onchain=excluded.exists_onchain,
                kind=excluded.kind,
                owner_program=excluded.owner_program,
                lamports=excluded.lamports,
                n_sigs_seen=COALESCE(excluded.n_sigs_seen, wallet_onchain_audit.n_sigs_seen),
                n_token_accts=COALESCE(excluded.n_token_accts, wallet_onchain_audit.n_token_accts),
                audited_at=excluded.audited_at
            """,
            rows,
        )
        conn.commit()
        n_done += len(batch)
        if n_done % 500 < BATCH:
            print(f"[audit-unaudited] {n_done}/{len(universe)} ({100*n_done/len(universe):.1f}%) — dead endpoints: {len(base._dead)}", flush=True)

    print(f"[audit-unaudited] complete. {n_done} wallets, {len(errors)} errors", flush=True)
    if errors[:5]:
        print(f"[audit-unaudited] first 5 errors: {errors[:5]}", flush=True)

    # ----- summary on just the newly-audited subset --------------------------
    placeholders = ",".join(["?"] * len(universe))
    print("")
    print("=" * 60)
    print("Breakdown — newly audited wallets only")
    print("-" * 60)
    summary = conn.execute(
        f"""
        SELECT kind, COUNT(*) FROM wallet_onchain_audit
        WHERE wallet IN ({placeholders}) GROUP BY kind ORDER BY 2 DESC
        """,
        universe,
    ).fetchall()
    by_kind_tvl = {}
    print(f"{'kind':<14}{'# wallets':>14}{'sum TVL (USD)':>22}")
    for kind, _ in summary:
        wallets = [w for (w,) in conn.execute(
            f"SELECT wallet FROM wallet_onchain_audit WHERE kind=? AND wallet IN ({placeholders})",
            (kind, *universe),
        )]
        s = sum(tvl_map.get(w, 0) for w in wallets)
        by_kind_tvl[kind] = s
    for kind, n in summary:
        print(f"{kind:<14}{n:>14,}{by_kind_tvl[kind]:>22,.0f}")
    print("=" * 60)

    # non-zero TVL among newly audited
    print("\nNewly-audited wallets with non-zero data.json TVL:")
    nz = [(w, tvl_map[w]) for w in universe if tvl_map.get(w, 0) > 0]
    nz.sort(key=lambda x: -x[1])
    print(f"  count = {len(nz)}")
    for w, t in nz[:20]:
        kind_row = conn.execute("SELECT kind, owner_program FROM wallet_onchain_audit WHERE wallet=?", (w,)).fetchone()
        kind, owner = kind_row if kind_row else ("?", None)
        print(f"  {w}  tvl={t:>14,.0f}  kind={kind}  owner={owner}")

    # candidate new PDAs (non-eoa among newly audited) grouped by owner
    print("\nNon-EOA owners discovered (potential protocol_pdas.json additions):")
    rows = conn.execute(
        f"""
        SELECT owner_program, kind, COUNT(*) FROM wallet_onchain_audit
        WHERE wallet IN ({placeholders}) AND kind != 'eoa' AND kind != 'nonexistent'
        GROUP BY owner_program, kind ORDER BY 3 DESC
        """,
        universe,
    ).fetchall()
    for owner, kind, n in rows:
        print(f"  {kind:<8} owner={owner}  count={n}")

    # Build JSON output
    out = {
        "total_newly_audited": n_done,
        "errors": len(errors),
        "breakdown": {kind: n for kind, n in summary},
        "sum_tvl_by_kind": by_kind_tvl,
        "nonzero_tvl_count": len(nz),
        "nonzero_tvl_top": [
            {"wallet": w, "tvl": t} for w, t in nz[:20]
        ],
        "nonexempt_owners": [
            {"owner": owner, "kind": kind, "count": n} for owner, kind, n in rows
        ],
    }
    print("\nJSON_RESULT_START")
    print(json.dumps(out, indent=2))
    print("JSON_RESULT_END")

    conn.close()
    return out


if __name__ == "__main__":
    try:
        audit()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(1)
