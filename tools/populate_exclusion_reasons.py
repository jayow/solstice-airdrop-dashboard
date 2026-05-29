"""Populate the wallet_exclusion_reasons table with descriptive context for
every currently-excluded wallet.

Categories (priority order):
  onchain_program > onchain_pda > nonexistent > walker_artifact > non_flare_eoa > heuristic_other

Idempotent — INSERT OR REPLACE on every row, so re-runs just refresh evidence.

Reads from:
  - data/solstice.db tables: wallets, wallet_onchain_audit
  - data/protocol_pdas.json
  - data/walker_artifacts.json

Writes to:
  - data/solstice.db table: wallet_exclusion_reasons
"""
import os, sys, json, sqlite3, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, 'data', 'solstice.db')

# 8 hand-identified non-flare EOAs (see CLAUDE.md: project_overnight_2026_05_16
# and exclusion scout facts).
NON_FLARE_EOA_WALLETS = [
    '6SnNkBnH9ifg6vRK25rZUcHdqj6CcWJd7yQwyjVNaarA',
    '2AgyBn9Q4RyH7nLv23ac6aZrBHBx2mPknuzbjgievBAy',
    'HPjy8JkEmNEDxogwdd1a3163N2buWj34i3fE2LyeRLEx',
    '5ZwWXKYWKdhSMCoBgi9MfLySvuCFncseZL9SKpKUXBNo',
    'Aymo81CYETaq1YQtio41nBDV3pNxoy2nRwyywXFEUjdG',
    'CWGGhiezrFdQg7b8gBERUJz6w9MceyShnV2ZY4YnnWJq',
    '4eu3y9CukaFR5XsMr5ELWHt74TCS5sPPBTuQeCdkWtGo',
    'C4f3Ld3fEmNs5ELKjbHf3yswi6nc2JoeRwXzJryr5CGG',
]

NON_FLARE_EOA_REASON = (
    "On-chain EOA holding positions but Solstice does not credit flares; "
    "including would push our total to >100% of Solstice's published total "
    "(empirical test). Likely treasury/operator/multisig signer/vault aggregator."
)


def ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS wallet_exclusion_reasons (
            wallet     TEXT PRIMARY KEY,
            category   TEXT NOT NULL,
            reason     TEXT NOT NULL,
            evidence   TEXT,
            added_at   INTEGER NOT NULL
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_excl_category ON wallet_exclusion_reasons(category)"
    )
    con.commit()


def upsert(con, wallet, category, reason, evidence, now):
    con.execute(
        "INSERT OR REPLACE INTO wallet_exclusion_reasons "
        "(wallet, category, reason, evidence, added_at) VALUES (?, ?, ?, ?, ?)",
        (wallet, category, reason, json.dumps(evidence) if evidence else None, now)
    )


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ensure_table(con)

    now = int(time.time())

    # Pre-load on-chain audit data (used as evidence + as a source of nonexistent/pda/program lists).
    audit_by_w = {}
    for r in con.execute(
        "SELECT wallet, kind, owner_program, n_sigs_seen, n_token_accts, audited_at "
        "FROM wallet_onchain_audit"
    ):
        audit_by_w[r['wallet']] = dict(r)

    # Pre-load manual labels from protocol_pdas.json.
    manual_pda_labels = {}
    p_pdas = os.path.join(ROOT, 'data', 'protocol_pdas.json')
    if os.path.exists(p_pdas):
        manual_pda_labels = json.load(open(p_pdas)).get('addresses') or {}

    # Pre-load walker_artifacts.json.
    walker_artifacts = {}   # address -> {reason}
    p_art = os.path.join(ROOT, 'data', 'walker_artifacts.json')
    if os.path.exists(p_art):
        for entry in json.load(open(p_art)).get('wallets') or []:
            if isinstance(entry, dict) and entry.get('address'):
                walker_artifacts[entry['address']] = entry

    # Track which categories each wallet ends up in (priority enforces single
    # category by inserting in priority order with INSERT OR REPLACE — later
    # categories beat earlier ones only if higher priority, so we go in
    # reverse priority order: lowest first, highest last).

    counts = {
        'onchain_program': 0,
        'onchain_pda': 0,
        'nonexistent': 0,
        'walker_artifact': 0,
        'non_flare_eoa': 0,
        'heuristic_other': 0,
    }

    # Track which wallets we've assigned so we can enforce priority strictly.
    assigned = {}   # wallet -> category (priority rank int)
    PRIORITY = {
        'heuristic_other': 1,
        'non_flare_eoa': 2,
        'walker_artifact': 3,
        'nonexistent': 4,
        'onchain_pda': 5,
        'onchain_program': 6,
    }

    def assign(wallet, category, reason, evidence):
        if wallet is None:
            return
        prev = assigned.get(wallet)
        if prev is not None and PRIORITY[prev] >= PRIORITY[category]:
            return   # existing assignment is higher priority — keep it
        if prev is not None:
            counts[prev] -= 1
        upsert(con, wallet, category, reason, evidence, now)
        assigned[wallet] = category
        counts[category] += 1

    # ----- heuristic_other: any wallets DB classification = pda_protocol where
    # wallet is NULL (the data-quality artifact). Lowest priority.
    for r in con.execute(
        "SELECT wallet, classification FROM wallets WHERE classification IN ('pda_protocol','pda','pda_or_uninit','institution')"
    ):
        w = r['wallet']
        if w is None:
            # Stray NULL row — record under heuristic_other with a synthetic id.
            assign('__NULL_WALLET__', 'heuristic_other',
                   "Stray DB row with NULL wallet column — data-quality artifact, no on-chain impact.",
                   {'classification': r['classification']})

    # ----- non_flare_eoa: the 8 hand-identified wallets.
    for w in NON_FLARE_EOA_WALLETS:
        a = audit_by_w.get(w, {})
        evidence = {
            'kind': a.get('kind'),
            'owner_program': a.get('owner_program'),
            'n_sigs_seen': a.get('n_sigs_seen'),
            'n_token_accts': a.get('n_token_accts'),
            'audited_at': a.get('audited_at'),
        }
        assign(w, 'non_flare_eoa', NON_FLARE_EOA_REASON, evidence)

    # ----- walker_artifact: from walker_artifacts.json.
    for w, entry in walker_artifacts.items():
        reason = entry.get('reason') or 'Walker artifact (unspecified)'
        evidence = {
            'walker_name': entry.get('walker') or entry.get('walker_name'),
            'root_cause': entry.get('root_cause') or entry.get('reason'),
        }
        assign(w, 'walker_artifact', reason, evidence)

    # ----- nonexistent: from wallet_onchain_audit kind='nonexistent'.
    for w, a in audit_by_w.items():
        if a.get('kind') != 'nonexistent':
            continue
        reason = "No on-chain account exists (verified 2026-05-29)"
        evidence = {'audited_at': a.get('audited_at')}
        assign(w, 'nonexistent', reason, evidence)

    # ----- onchain_pda: from wallet_onchain_audit kind='pda' OR
    # protocol_pdas.json entries with source='auto_onchain_audit' or kind='pda'.
    onchain_pda_wallets = set()
    for w, a in audit_by_w.items():
        if a.get('kind') == 'pda':
            onchain_pda_wallets.add(w)
    for w, m in manual_pda_labels.items():
        if m.get('source') == 'auto_onchain_audit' or m.get('kind') == 'pda':
            onchain_pda_wallets.add(w)
    for w in onchain_pda_wallets:
        a = audit_by_w.get(w, {})
        owner = a.get('owner_program') or (manual_pda_labels.get(w) or {}).get('label') or 'unknown'
        reason = f"On-chain owner: {owner} (verified by wallet_onchain_audit 2026-05-29)"
        evidence = {
            'owner': a.get('owner_program'),
            'kind': a.get('kind') or 'pda',
            'audited_at': a.get('audited_at'),
        }
        # Pull in manual label/protocol if present.
        m = manual_pda_labels.get(w)
        if m:
            evidence['manual_label'] = m.get('label')
            evidence['manual_protocol'] = m.get('protocol')
        assign(w, 'onchain_pda', reason, evidence)

    # ----- onchain_program: from wallet_onchain_audit kind='program'.
    for w, a in audit_by_w.items():
        if a.get('kind') != 'program':
            continue
        reason = f"Address is an on-chain program (kind=program, owner={a.get('owner_program')})"
        evidence = {
            'owner': a.get('owner_program'),
            'kind': 'program',
            'audited_at': a.get('audited_at'),
        }
        assign(w, 'onchain_program', reason, evidence)

    con.commit()

    # Final breakdown.
    total = sum(counts.values())
    print(f"\nPopulated wallet_exclusion_reasons: {total:,} rows")
    for cat in ['onchain_program', 'onchain_pda', 'nonexistent',
                'walker_artifact', 'non_flare_eoa', 'heuristic_other']:
        print(f"  {cat:<20s} {counts[cat]:>6,}")

    # Verify the 8 non_flare_eoa wallets are present.
    print("\nVerification — 8 non_flare_eoa wallets:")
    for w in NON_FLARE_EOA_WALLETS:
        r = con.execute(
            "SELECT category, reason FROM wallet_exclusion_reasons WHERE wallet=?",
            (w,)
        ).fetchone()
        if r:
            print(f"  {w[:12]}…  category={r[0]}")
        else:
            print(f"  {w[:12]}…  MISSING")

    con.close()


if __name__ == '__main__':
    main()
