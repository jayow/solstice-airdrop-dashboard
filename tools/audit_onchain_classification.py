"""On-chain classification audit for every wallet in quest_cache.

For each wallet:
  - getMultipleAccounts (batched 100) → exists / owner / lamports / executable
  - kind: 'nonexistent' | 'eoa' | 'pda' | 'program'
For wallets with TVL > $10,000:
  - getSignaturesForAddress(limit=100) → n_sigs_seen
  - getTokenAccountsByOwner (token program) → n_token_accts

Writes results to wallet_onchain_audit table in data/solstice.db.
Prints a final summary by kind with summed TVL (joined against server/data.json).
"""
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "solstice.db")
DATA_JSON = os.path.join(ROOT, "server", "data.json")
ENV_PATH = os.path.join(ROOT, ".env")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# ---------- env / rpc plumbing ---------------------------------------------------

def load_endpoints():
    """Read .env, return list of full RPC URLs. Helius first, then extras."""
    endpoints = []
    helius = None
    extras = []
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "HELIUS_API_KEY":
                if v.startswith("http"):
                    helius = v
                else:
                    helius = f"https://mainnet.helius-rpc.com/?api-key={v}"
            elif k in ("quicknode", "chainstack", "alchemy", "solsticerpc"):
                if v.startswith("http"):
                    extras.append(v)
    if helius:
        endpoints.append(helius)
    endpoints.extend(extras)
    if not endpoints:
        raise RuntimeError("No RPC endpoints found in .env")
    return endpoints


ENDPOINTS = load_endpoints()
_dead = set()
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def rpc(method, params, max_retries=6, timeout=45):
    """Round-robin across endpoints with retry on 429/5xx and quota errors."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for attempt in range(max_retries):
        for ep in ENDPOINTS:
            if ep in _dead:
                continue
            try:
                req = urllib.request.Request(
                    ep,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
                    raw = r.read()
                    j = json.loads(raw)
                    if "error" in j:
                        code = j["error"].get("code")
                        msg = str(j["error"].get("message", ""))
                        if code in (-32429, -32413) or "quota" in msg.lower() or "rate" in msg.lower():
                            _dead.add(ep)
                            last_err = j["error"]
                            continue
                        # genuine RPC error (e.g. bad pubkey for one wallet) — return so caller can decide
                        return j
                    return j
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503, 504):
                    time.sleep(min(4, 0.4 * (2 ** attempt)))
                    continue
                # 4xx other than 429 — likely endpoint-specific; try next
                continue
            except Exception as e:
                last_err = e
                continue
        time.sleep(min(4, 0.3 * (2 ** attempt)))
    return {"error": {"message": f"all endpoints failed: {last_err}"}}


# ---------- audit ---------------------------------------------------------------

def ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_onchain_audit (
            wallet         TEXT PRIMARY KEY,
            exists_onchain INTEGER,
            kind           TEXT,
            owner_program  TEXT,
            lamports       INTEGER,
            n_sigs_seen    INTEGER,
            n_token_accts  INTEGER,
            audited_at     INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_audit_kind ON wallet_onchain_audit(kind)")
    conn.commit()


def classify(account):
    """account = element of getMultipleAccounts result.value (or None if missing)."""
    if account is None:
        return False, "nonexistent", None, 0, False
    owner = account.get("owner")
    lamports = account.get("lamports", 0)
    executable = bool(account.get("executable", False))
    if executable:
        kind = "program"
    elif owner == SYSTEM_PROGRAM:
        kind = "eoa"
    else:
        kind = "pda"
    return True, kind, owner, lamports, executable


def load_universe(conn):
    rows = conn.execute("SELECT DISTINCT wallet FROM quest_cache").fetchall()
    return [r[0] for r in rows if r[0]]


def load_tvl_map():
    """wallet -> tvl (USD) from server/data.json"""
    if not os.path.exists(DATA_JSON):
        return {}
    d = json.load(open(DATA_JSON))
    out = {}
    for r in d.get("records", []):
        w = r.get("wallet")
        if w:
            out[w] = float(r.get("tvl", 0) or 0)
    return out


def load_prev_classification(conn):
    rows = conn.execute("SELECT wallet, classification FROM wallets").fetchall()
    return {w: c for w, c in rows}


def fetch_extra_for_high_tvl(wallet):
    """Returns (n_sigs_seen, n_token_accts) for one wallet. Defensive on errors."""
    n_sigs = None
    n_tok = None
    j = rpc("getSignaturesForAddress", [wallet, {"limit": 100}])
    if isinstance(j, dict) and "result" in j and isinstance(j["result"], list):
        n_sigs = len(j["result"])
    j2 = rpc("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_PROGRAM}, {"encoding": "base64"}])
    if isinstance(j2, dict) and "result" in j2:
        val = j2["result"].get("value") if isinstance(j2["result"], dict) else None
        if isinstance(val, list):
            n_tok = len(val)
    return n_sigs, n_tok


def audit():
    conn = sqlite3.connect(DB_PATH)
    ensure_table(conn)
    universe = load_universe(conn)
    tvl_map = load_tvl_map()
    prev_class = load_prev_classification(conn)

    print(f"[audit] {len(universe)} wallets to audit", flush=True)
    print(f"[audit] {len(tvl_map)} wallets with TVL records in data.json", flush=True)
    print(f"[audit] {sum(1 for w in universe if tvl_map.get(w, 0) > 10000)} wallets above $10k TVL (extra sigs+tokens fetch)", flush=True)

    audited_at = int(time.time())
    errors = []
    n_done = 0
    BATCH = 100

    for i in range(0, len(universe), BATCH):
        batch = universe[i:i + BATCH]
        j = rpc("getMultipleAccounts", [batch, {"encoding": "base64"}])
        if not isinstance(j, dict) or "result" not in j:
            errors.append((i, str(j)[:200]))
            print(f"[audit] batch {i} failed: {str(j)[:200]}", flush=True)
            continue
        values = j["result"].get("value") if isinstance(j["result"], dict) else None
        if values is None:
            errors.append((i, "no value in result"))
            continue
        rows = []
        for wallet, acct in zip(batch, values):
            exists, kind, owner, lamports, _ = classify(acct)
            n_sigs = None
            n_tok = None
            if tvl_map.get(wallet, 0) > 10000:
                try:
                    n_sigs, n_tok = fetch_extra_for_high_tvl(wallet)
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
        if n_done % 1000 < BATCH:
            print(f"[audit] {n_done}/{len(universe)} ({100*n_done/len(universe):.1f}%) — dead endpoints: {len(_dead)}", flush=True)

    print(f"[audit] complete. {n_done} wallets, {len(errors)} errors", flush=True)
    if errors[:5]:
        print(f"[audit] first 5 errors: {errors[:5]}", flush=True)

    # ----- summary -------------------------------------------------------------
    summary = conn.execute(
        "SELECT kind, COUNT(*) FROM wallet_onchain_audit GROUP BY kind ORDER BY 2 DESC"
    ).fetchall()
    print("")
    print("=" * 60)
    print(f"{'kind':<14}{'# wallets':>14}{'sum TVL (USD)':>22}")
    print("-" * 60)
    by_kind_tvl = {}
    for kind, _ in summary:
        wallets = [w for (w,) in conn.execute("SELECT wallet FROM wallet_onchain_audit WHERE kind=?", (kind,))]
        s = sum(tvl_map.get(w, 0) for w in wallets)
        by_kind_tvl[kind] = s
    for kind, n in summary:
        print(f"{kind:<14}{n:>14,}{by_kind_tvl[kind]:>22,.0f}")
    print("=" * 60)
    print("")

    # Top 10 anomalous wallets per kind
    for kind in ("pda", "program", "nonexistent"):
        rows = conn.execute("SELECT wallet FROM wallet_onchain_audit WHERE kind=?", (kind,)).fetchall()
        ranked = sorted(((w, tvl_map.get(w, 0)) for (w,) in rows), key=lambda x: -x[1])[:10]
        print(f"-- top 10 {kind} by TVL --")
        for w, t in ranked:
            cls = prev_class.get(w, "?")
            print(f"  {w}  tvl={t:>16,.0f}  prev_class={cls}")
        print("")

    # real_user wallets that look anomalous on-chain
    print("-- real_user wallets whose on-chain kind is not 'eoa' --")
    qry = """
        SELECT a.wallet, a.kind, a.owner_program, w.classification
        FROM wallet_onchain_audit a
        JOIN wallets w USING(wallet)
        WHERE w.classification='real_user' AND a.kind != 'eoa'
        ORDER BY a.kind
    """
    rows = conn.execute(qry).fetchall()
    if not rows:
        print("  (none)")
    for w, kind, owner, cls in rows:
        t = tvl_map.get(w, 0)
        print(f"  {w}  kind={kind:<11}  owner={owner}  tvl={t:>14,.0f}")
    print("")

    conn.close()


if __name__ == "__main__":
    try:
        audit()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(1)
