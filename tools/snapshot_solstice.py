"""Daily snapshot of Solstice public surfaces — docs, dashboard, marketing, API.

For each tracked URL/path:
  1. Fetch raw response (HTML, JS, or JSON)
  2. For HTML: extract visible text (strip tags, normalize whitespace)
  3. For JS chunks: extract user-facing string literals only
  4. For JSON: pretty-print sorted
  5. Save under data/solstice_snapshots/<YYYY-MM-DD>/<path>

Designed to be small + deterministic so day-over-day diffs surface
real content changes, not deploy-hash noise or transient values.

Run: python3 tools/snapshot_solstice.py
"""
import os, sys, re, json, hashlib, datetime, requests
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / 'data' / 'solstice_snapshots'
TODAY = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d')
OUTDIR = SNAPSHOTS_DIR / TODAY
OUTDIR.mkdir(parents=True, exist_ok=True)

UA = 'Mozilla/5.0 (compatible; SolsticeMonitor/1.0)'

# Targets:
#   ('save_path', 'url', 'mode')
#   mode: 'text' = extract visible text from HTML
#         'json' = pretty-print + sort keys
#         'js'   = extract user-facing string literals from JS chunk
#         'html-titles' = just the route list / titles
TARGETS = [
    # === Docs (GitBook) ===
    ('docs/site-index.json',                       'https://docs.solstice.finance/~gitbook/site-index', 'json'),
    ('docs/_homepage.txt',                         'https://docs.solstice.finance/', 'text'),
    ('docs/overview_slx-tge-mechanics.txt',        'https://docs.solstice.finance/overview/slx-tge-mechanics', 'text'),
    ('docs/overview_track-record.txt',             'https://docs.solstice.finance/overview/track-record', 'text'),
    ('docs/overview_yield-sources.txt',            'https://docs.solstice.finance/overview/yield-sources', 'text'),
    ('docs/overview_solstice-staking.txt',         'https://docs.solstice.finance/overview/solstice-staking', 'text'),
    ('docs/overview_ambassador-program.txt',       'https://docs.solstice.finance/overview/ambassador-program', 'text'),
    ('docs/overview_resources.txt',                'https://docs.solstice.finance/overview/resources', 'text'),
    ('docs/users_usx.txt',                         'https://docs.solstice.finance/solstice-for-users/usx', 'text'),
    ('docs/users_eusx.txt',                        'https://docs.solstice.finance/solstice-for-users/eusx-powered-by-yieldvault', 'text'),
    ('docs/users_slx.txt',                         'https://docs.solstice.finance/solstice-for-users/slx', 'text'),
    ('docs/users_slx_tokenomics.txt',              'https://docs.solstice.finance/solstice-for-users/slx/tokenomics', 'text'),
    ('docs/users_stslx.txt',                       'https://docs.solstice.finance/solstice-for-users/stslx', 'text'),
    ('docs/users_flares.txt',                      'https://docs.solstice.finance/solstice-for-users/flares', 'text'),
    ('docs/users_flares_season-1.txt',             'https://docs.solstice.finance/solstice-for-users/flares/season-1', 'text'),
    ('docs/users_flares_season-2.txt',             'https://docs.solstice.finance/solstice-for-users/flares/season-2', 'text'),
    ('docs/users_flares_claims.txt',               'https://docs.solstice.finance/solstice-for-users/flares/claims', 'text'),
    ('docs/users_flares_claims_vesting.txt',       'https://docs.solstice.finance/solstice-for-users/flares/claims/vesting', 'text'),
    ('docs/builders_apis.txt',                     'https://docs.solstice.finance/solstice-for-builders/apis', 'text'),
    ('docs/builders_smart-contracts.txt',          'https://docs.solstice.finance/solstice-for-builders/smart-contracts', 'text'),

    # === Marketing site ===
    ('marketing/_homepage.txt',                    'https://solstice.finance/', 'text'),

    # === App dashboard ===
    ('app/earn-flares.html',                       'https://app.solstice.finance/earn-flares', 'html-titles'),
    ('app/dashboard.html',                         'https://app.solstice.finance/dashboard', 'html-titles'),

    # === Public API endpoints ===
    # Each captured as raw JSON. Diffs flag content changes (multipliers, dates,
    # allocation %, quest configs); routine numeric drift (totalFlares,
    # totalUsers, peg values) won't match the high-signal regex but will show
    # up in the diff body for context.
    ('api/app_protocol.json',                      'https://app.solstice.finance/api/protocol', 'json'),
    ('api/app_flares-analytics.json',              'https://app.solstice.finance/api/flares/analytics', 'json'),
    ('api/app_flares-quests.json',                 'https://app.solstice.finance/api/flares/quests', 'json'),
    ('api/app_rewards-global.json',                'https://app.solstice.finance/api/rewards/global/analytics', 'json'),
    ('api/app_rewards-settings.json',              'https://app.solstice.finance/api/rewards/settings', 'json'),
    ('api/app_auth-status.json',                   'https://app.solstice.finance/api/auth/status', 'json'),
    ('api/app_session-check.json',                 'https://app.solstice.finance/api/session/check', 'json'),
    # Clique attestation health (any change = backend redeploy)
    ('api/clique_health.json',                     'https://acs-v4.clique.tech/health', 'json'),
    # Solstice institutional API gateway (auth required → 401 is the "ok" state)
    ('api/institutional_v1-info-status.txt',       'https://api.solstice.finance/v1/info', 'status-only'),
    # Claim site
    ('api/claim_account-info-status.txt',          'https://claim.solstice.finance/api/account/info', 'status-only'),

    # === Claim site ===
    ('claim/_homepage.html',                       'https://claim.solstice.finance/', 'html-titles'),
    ('claim/_flow.html',                           'https://claim.solstice.finance/flow', 'html-titles'),

    # === Registration site ===
    ('registration/_end.html',                     'https://registration.solstice.finance/', 'html-titles'),
]

# Chunk-list targets: capture which JS chunks each app page loads (deploy hash changes signal a redeploy)
CHUNK_LIST_TARGETS = [
    ('app/earn-flares-chunks.txt',      'https://app.solstice.finance/earn-flares'),
    ('app/dashboard-chunks.txt',        'https://app.solstice.finance/dashboard'),
    ('claim/flow-chunks.txt',           'https://claim.solstice.finance/flow'),
]

# JS chunks to snapshot full content (only the ones that hold UI-text we care about)
# Auto-detected from the page HTML — patterns match the page-specific bundle.
# For claim.solstice.finance/flow (turbopack-built): no app/<route>/page-*.js pattern; instead we
# capture ALL chunks loaded by /flow and concat their extracted UI strings, with the chunk list
# itself (already in chunks.txt) flagging any deploy-hash redirection.
JS_CHUNK_PAGE_PATTERNS = [
    ('app/earn-flares-page-chunk.txt',   'https://app.solstice.finance/earn-flares',
     re.compile(r'/_next/static/chunks/app/earn-flares/page-[a-f0-9]+\.js')),
]

# Sites where we should snapshot ALL loaded chunks' UI strings concatenated (no per-route page chunk).
ALL_CHUNK_SNAPSHOTS = [
    ('claim/flow-all-chunks.txt',     'https://claim.solstice.finance/flow'),
]

def fetch(url, timeout=20):
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return f"__FETCH_ERROR__: {e}"

def fetch_with_status(url, timeout=20):
    """Returns 'HTTP <code> [<body>]' — useful for endpoints where auth-state itself is the signal."""
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=timeout)
        body = r.text[:500] if r.text else ''
        return f"HTTP {r.status_code}\nbody: {body}"
    except Exception as e:
        return f"__FETCH_ERROR__: {e}"

def html_to_text(html):
    """Strip tags, drop script/style content, normalize whitespace.
    Goal: stable string we can diff day-over-day."""
    if html.startswith('__FETCH_ERROR__'):
        return html
    # Drop <script>, <style>, <noscript> content entirely
    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL|re.IGNORECASE)
    # Strip CSS (within <style> got dropped, but any leftover @-rules and class selectors)
    html = re.sub(r'@media[^{]+\{[^}]+\}', '', html)
    html = re.sub(r'\.[a-z][\w-]*\s*\{[^}]+\}', '', html)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&#x?[0-9a-f]+;', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'&\w+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    # GitBook docs have a fingerprint hash + last-updated timestamp that changes every load — strip those
    text = re.sub(r'\$RT=performance\.now\(\)[^\.]*\.', '', text)
    text = re.sub(r'Last updated \d+ \w+ ago', 'Last updated (relative time stripped)', text)
    return text

def js_strings(js, min_len=5):
    """Extract user-facing string literals from a JS chunk.

    Uses lookarounds so consecutive JSX literals like `["A","B","C"]` all get
    captured (a consuming regex would lose every-other string)."""
    if js.startswith('__FETCH_ERROR__'):
        return js
    out = set()
    # Lookaround-bounded literal: "..." where ... has no quote/backslash
    # min_len=5 catches short UI-critical strings like "3 Months", "01.08.26"
    pat = r'(?<=")([^"\\]{%d,500})(?=")' % min_len
    for m in re.finditer(pat, js):
        s = m.group(1)
        # Drop obvious non-UI strings
        if any(s.startswith(p) for p in ('http', 'data:', 'rgb(', '/_next', 'arn:', 'function ', 'class ', 'M ', '0 0 ')):
            continue
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', s) and len(s) < 30:  # single identifiers
            continue
        if re.match(r'^[0-9a-fA-F\-]+$', s) and len(s) > 20:  # hashes/uuids
            continue
        # Allow date-like patterns (01.08.26, 2026-08-01, etc.) and dollar amounts
        is_datelike = bool(re.match(r'^\d{1,4}[.\-/]\d{1,2}[.\-/]\d{1,4}$', s))
        is_dollarlike = bool(re.match(r'^\$[\d.,]+[KMBkmb]?$', s))
        if not is_datelike and not is_dollarlike:
            if re.match(r'^[\d.,]+$', s):  # pure numbers
                continue
            if re.match(r'^[\d{}()\[\],;:]+$', s):  # syntax-only
                continue
        # Accept if has letter or is dollar/date
        has_alpha = bool(re.search(r'[A-Za-z]', s))
        if not has_alpha and not is_datelike and not is_dollarlike:
            continue
        # Drop CSS-only fragments
        if re.match(r'^[\d.a-z\s%#-]+$', s) and any(t in s for t in ('px ', 'em ', '%', 'rgb')):
            continue
        out.add(s)
    return '\n'.join(sorted(out))

def text_normalize(text):
    """For text snapshots: strip dynamic numbers + paragraph-split for line-level diffs."""
    if text.startswith('__FETCH_ERROR__'):
        return text
    # Solstice docs show "Last updated N (minutes|hours|days) ago" — normalize
    text = re.sub(r'Last updated \d+\s*(minute|hour|day|week|month|second)s?\s*ago', 'Last updated (X ago)', text)
    # Heuristic sentence/section split so diffs are line-by-line instead of one-giant-line.
    # Split on sentence boundaries + section headings (capital-led words after period).
    text = re.sub(r'(?<=[.!?])\s+(?=[A-Z])', '\n', text)
    text = re.sub(r'\s{2,}', '\n', text)  # multi-space collapses to newline
    # Re-collapse whitespace inside each line
    lines = [re.sub(r'\s+', ' ', l).strip() for l in text.splitlines()]
    return '\n'.join(l for l in lines if l)

# Known-volatile fields where the *value* drifts every request — neutralize
# so day-over-day diffs only fire on structural / config changes.
VOLATILE_FIELDS = {
    'timestamp', 'time', 'lastUpdated', 'updatedAt',
    'totalFlare', 'totalFlares', 'totalUsers',
    'eusxSupply', 'usxSupply', 'eusxBacking', 'usxBacking', 'eusxPrice',
    'apy', 'compoundApy', 'monthlyAverageApy',
    'stslxRedemptionRate', 'stslxCompoundApy', 'stslxCycleApy',
    'tvlMonthlyChange', 'tvlDailyChange', 'sharpeRatio',
    'campaignChange', 'supplyBurned', 'boughtBack', 'campaignSlxAllocationChange',
}

def _neutralize_volatile(d):
    if isinstance(d, dict):
        return {k: ('<volatile>' if k in VOLATILE_FIELDS else _neutralize_volatile(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_neutralize_volatile(x) for x in d]
    return d

def json_normalize(text):
    """For API JSON: pretty-print sorted keys + neutralize known volatile values."""
    if text.startswith('__FETCH_ERROR__'):
        return text
    try:
        d = json.loads(text)
        d = _neutralize_volatile(d)
        return json.dumps(d, indent=2, sort_keys=True)
    except Exception:
        return text

def write_snapshot(rel_path, content):
    path = OUTDIR / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path

def main():
    print(f"[snapshot_solstice] saving to {OUTDIR}")
    n_ok = 0
    n_err = 0
    for save_path, url, mode in TARGETS:
        if mode == 'status-only':
            content = fetch_with_status(url)
        else:
            raw = fetch(url)
            if mode == 'text' or mode == 'html-titles':
                content = text_normalize(html_to_text(raw))
            elif mode == 'json':
                content = json_normalize(raw)
            elif mode == 'js':
                content = js_strings(raw)
            else:
                content = raw
        write_snapshot(save_path, content)
        ok = not content.startswith('__FETCH_ERROR__')
        n_ok += int(ok); n_err += int(not ok)
        print(f"  {'✓' if ok else '✗'} {save_path}  ({len(content)} chars)")

    # Chunk lists (helps detect deploys)
    for save_path, url in CHUNK_LIST_TARGETS:
        raw = fetch(url)
        chunks = sorted(set(re.findall(r'/_next/static/chunks/[a-zA-Z0-9_/-]+\.js', raw)))
        # Strip the unique chunk hash to detect content vs hash-only changes
        chunks_with_hash = '\n'.join(chunks)
        write_snapshot(save_path, chunks_with_hash)
        print(f"  ✓ {save_path}  ({len(chunks)} chunks)")

    # Auto-detected page chunks
    for save_path, page_url, pat in JS_CHUNK_PAGE_PATTERNS:
        page_html = fetch(page_url)
        m = pat.search(page_html)
        if not m:
            write_snapshot(save_path, '__NO_PAGE_CHUNK_DETECTED__')
            print(f"  ✗ {save_path}: page chunk not found in HTML")
            n_err += 1
            continue
        chunk_url = 'https://' + page_url.split('/')[2] + m.group(0)
        chunk_js = fetch(chunk_url)
        content = js_strings(chunk_js)
        write_snapshot(save_path, f"# chunk: {m.group(0)}\n\n{content}")
        n_ok += 1
        print(f"  ✓ {save_path}  ({len(content)} chars, chunk={m.group(0)[-25:]})")

    # All-chunk snapshots (concat UI strings across every chunk loaded by the page)
    for save_path, page_url in ALL_CHUNK_SNAPSHOTS:
        page_html = fetch(page_url)
        chunks = sorted(set(re.findall(r'/_next/static/chunks/[a-zA-Z0-9_/-]+\.js', page_html)))
        host = 'https://' + page_url.split('/')[2]
        all_strings = set()
        for c in chunks:
            js = fetch(host + c, timeout=15)
            for s in js_strings(js).split('\n'):
                if s: all_strings.add(s)
        content = '\n'.join(sorted(all_strings))
        write_snapshot(save_path, content)
        n_ok += 1
        print(f"  ✓ {save_path}  ({len(all_strings)} unique strings from {len(chunks)} chunks)")

    print(f"\n[snapshot_solstice] done. ok={n_ok} err={n_err}")

if __name__ == '__main__':
    main()
