"""One-shot import: legacy file-based data → SQLite (data/solstice.db).

Source files:
  data/solstice_registration/addresses.txt          -> wallets.is_s1
  data/solstice_registration/fee_payers_with_cohort.csv -> wallets.cohort
  data/wallet_classification_offline.csv           -> wallets.classification + partner-footprint flags
  data/quest_results.jsonl                         -> wallet_quests (+ wallet discovery)
  data/quest_cache/<key>/<wallet>.json             -> quest_cache
  data/s2_*_flares.json                            -> walker_outputs
  data/flares_snapshots.jsonl                      -> flares_snapshots

Safe to re-run: uses INSERT OR REPLACE everywhere.
"""
import os, sys, json, csv, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, 'data')

WALKER_FILE_MAP = {
    's2_lp_flares.json':              'walk_s2_lp',
    's2_kamino_flares.json':          'walk_s2_kamino',
    's2_kamino_strategy_flares.json': 'walk_s2_kamino_strategy',
    's2_loopscale_flares.json':       'walk_s2_loopscale',
    's2_orca_flares.json':            'walk_s2_orca',
    's2_raydium_flares.json':         'walk_s2_raydium',
}


def t(): return time.time()


def migrate_wallets():
    print(f'[wallets] importing metadata...', flush=True)
    t0 = t()
    s1 = set()
    p = os.path.join(DATA, 'solstice_registration', 'addresses.txt')
    if os.path.exists(p):
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if line: s1.add(line)

    cohort = {}
    p = os.path.join(DATA, 'solstice_registration', 'fee_payers_with_cohort.csv')
    if os.path.exists(p):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                if r.get('wallet'): cohort[r['wallet']] = r.get('cohort') or ''

    classification = {}
    p = os.path.join(DATA, 'wallet_classification_offline.csv')
    if os.path.exists(p):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                classification[r['wallet']] = {
                    'class': r.get('classification', 'unclassified'),
                    'in_partner_footprint': r.get('in_partner_footprint_s2') == 'True',
                    'in_exponent': r.get('in_exponent_signers') == 'True',
                    'n_protocols': int(r.get('n_protocols_with_flares') or 0),
                }

    union = s1 | set(cohort.keys()) | set(classification.keys())
    with db.txn() as c:
        for w in union:
            cls = classification.get(w, {})
            c.execute('INSERT OR REPLACE INTO wallets '
                      '(wallet, is_s1, cohort, classification, in_partner_footprint, in_exponent, n_protocols) '
                      'VALUES (?,?,?,?,?,?,?)',
                      (w, 1 if w in s1 else 0, cohort.get(w, ''),
                       cls.get('class', 'unclassified'),
                       1 if cls.get('in_partner_footprint') else 0,
                       1 if cls.get('in_exponent') else 0,
                       cls.get('n_protocols', 0)))
    print(f'  {len(union):,} wallets imported ({t()-t0:.1f}s)')


def migrate_wallet_quests():
    print(f'[wallet_quests] importing per-wallet flares...', flush=True)
    t0 = t()
    src = os.path.join(DATA, 'quest_results.jsonl')
    if not os.path.exists(src):
        print('  no quest_results.jsonl, skipping'); return
    n_wallets = 0; n_rows = 0
    with open(src) as f, db.txn() as c:
        for line in f:
            try: r = json.loads(line)
            except: continue
            w = r.get('wallet')
            if not w: continue
            n_wallets += 1
            flares = r.get('flares', {}) or {}
            # Ensure wallet exists in `wallets` table (insert with defaults if not)
            c.execute('INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, ?)',
                      (w, 'unclassified'))
            for q, v in flares.items():
                f_v = float(v or 0)
                if f_v == 0: continue
                c.execute('INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                          'VALUES (?,?,?,?,strftime("%s","now"))',
                          (w, q, f_v, 'migrated'))
                n_rows += 1
    print(f'  {n_wallets:,} wallets, {n_rows:,} non-zero quest rows ({t()-t0:.1f}s)')


def migrate_quest_cache():
    print(f'[quest_cache] importing raw extract caches...', flush=True)
    t0 = t()
    base = os.path.join(DATA, 'quest_cache')
    if not os.path.isdir(base):
        print('  no quest_cache dir, skipping'); return
    n = 0
    with db.txn() as c:
        for sub in os.listdir(base):
            p = os.path.join(base, sub)
            if not os.path.isdir(p): continue
            for fname in os.listdir(p):
                if not fname.endswith('.json'): continue
                wallet = fname[:-5]
                try:
                    blob = json.load(open(os.path.join(p, fname)))
                except: continue
                raw = blob.get('raw') or {}
                c.execute('INSERT OR REPLACE INTO quest_cache '
                          '(wallet, quest_key, raw_json, watermark_slot, watermark_ts, extracted_at, schema_version) '
                          'VALUES (?,?,?,?,?,?,?)',
                          (wallet, sub, json.dumps(raw, separators=(',',':')),
                           int(blob.get('watermark_slot') or 0),
                           int(blob.get('watermark_ts') or 0),
                           int(blob.get('extracted_at_ts') or time.time()),
                           int(blob.get('schema_version') or 1)))
                n += 1
                if n % 20000 == 0: print(f'    {n:,}...', flush=True)
    print(f'  {n:,} cache rows imported ({t()-t0:.1f}s)')


def migrate_walker_outputs():
    print(f'[walker_outputs] importing walker results...', flush=True)
    t0 = t()
    total = 0
    with db.txn() as c:
        for fname, walker in WALKER_FILE_MAP.items():
            p = os.path.join(DATA, fname)
            if not os.path.exists(p): continue
            try: d = json.load(open(p))
            except: continue
            for wallet, per_quest in d.items():
                for quest, flares in per_quest.items():
                    f_v = float(flares or 0)
                    if f_v == 0: continue
                    c.execute('INSERT OR REPLACE INTO walker_outputs '
                              '(walker, wallet, quest, flares, refreshed_at) '
                              'VALUES (?,?,?,?,strftime("%s","now"))',
                              (walker, wallet, quest, f_v))
                    total += 1
    print(f'  {total:,} walker rows imported ({t()-t0:.1f}s)')


def migrate_snapshots():
    print(f'[flares_snapshots] importing daily snapshots...', flush=True)
    t0 = t()
    p = os.path.join(DATA, 'flares_snapshots.jsonl')
    if not os.path.exists(p):
        print('  no snapshots file, skipping'); return
    n = 0
    with open(p) as f, db.txn() as c:
        for line in f:
            try: r = json.loads(line)
            except: continue
            ts = r.get('ts')
            ts_int = int(__import__('datetime').datetime.fromisoformat(ts).timestamp()) if isinstance(ts, str) else int(ts or time.time())
            c.execute('INSERT OR REPLACE INTO flares_snapshots '
                      '(ts, date_utc, source, universe_size, grand_total, quest_totals_json) '
                      'VALUES (?,?,?,?,?,?)',
                      (ts_int, r.get('date_utc'),
                       r.get('source', 'our_framework'),
                       int(r.get('universe_size') or 0),
                       float(r.get('grand_total') or 0),
                       json.dumps(r.get('quest_totals', {}))))
            n += 1
    print(f'  {n:,} snapshot rows imported ({t()-t0:.1f}s)')


def main():
    print(f'\n=== MIGRATION: legacy files -> data/solstice.db ===\n')
    db.init()
    print('schema initialized\n')
    migrate_wallets()
    migrate_wallet_quests()
    migrate_quest_cache()
    migrate_walker_outputs()
    migrate_snapshots()
    print(f'\n=== HEALTH ===')
    for tbl, n in db.health().items():
        print(f'  {tbl:<22s} {n:>10,}')


if __name__ == '__main__':
    main()
