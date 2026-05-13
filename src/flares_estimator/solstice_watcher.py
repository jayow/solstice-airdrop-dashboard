"""Hourly Solstice/Exponent ecosystem watcher.

Polls 5 sources every hour, diffs against previous state stored in
data/monitor_state.json, and prints one stdout line per detected change.
When run under the Monitor tool, each stdout line becomes a chat notification —
so silent hours stay silent, real signals fire alerts.

Sources:
  1. GitHub: Solstice-Labs-Official + exponent-finance push events (per-repo head SHA)
  2. npm: @exponent-labs/{exponent-sdk, exponent-pda, solstice-idl} latest versions
  3. HTTP endpoints: claim.solstice.finance, app.solstice.finance, v2-beta.exponent.finance
  4. On-chain wallet sigs: Thomas Proust, Solstice multisig, 85F1 claim PDA
  5. On-chain balances: SLX treasury ATA (alert if drops from 1B)

Run via:
  Monitor(persistent=True, command="python3 -u src/flares_estimator/solstice_watcher.py")
"""
import os, sys, json, time, hashlib, subprocess
from datetime import datetime, UTC

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc

import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE = os.path.join(ROOT, 'data', 'monitor_state.json')

POLL_INTERVAL = 3600   # 1 hour

# What to watch
GH_REPOS = [
    'Solstice-Labs-Official/squads-public-v4-client',
    'Solstice-Labs-Official/DefiLlama-Adapters',
    'Solstice-Labs-Official/slx-metadata',
    'Solstice-Labs-Official/stslx-metadata',
    'Solstice-Labs-Official/eusx-metadata',
    'exponent-finance/exponent-core',
    'exponent-finance/strategy-recipes',
    'exponent-finance/exponent-audits',
]
NPM_PACKAGES = [
    '@exponent-labs/exponent-sdk',
    '@exponent-labs/exponent-pda',
    '@exponent-labs/solstice-idl',
    '@exponent-labs/market-three-math',
]
HTTP_ENDPOINTS = [
    'https://claim.solstice.finance',
    'https://app.solstice.finance',
    'https://v2-beta.exponent.finance',
    'https://raw.githubusercontent.com/Solstice-Labs-Official/slx-metadata/refs/heads/main/metadata.json',
]
ONCHAIN_WALLETS = {
    'thomas_proust':       'xLsjtG4aVE8bwupaQrcEjcYdta1KXNjvR9cqmtVYiW5',
    'solstice_multisig':   'CYr14TrXD5MAertRqcR58KirPz7XoHGZjfBsfn5M2voh',
    'claim_85F1':          '85F1bj5k85LZxzHM35epKtHD5E11HcYsxLpV8VbyT6od',
    'yield_vault_program': 'eUSXyKoZ6aGejYVbnp3wtWQ1E8zuokLAJPecPxxtgG3',
}
SLX_TREASURY_ATA = '2SWgHpXxdL5ZPsiWhZWrZPQrdZWm3im8WwzisxCkBKoM'
SLX_MINT         = 'SLXdx4BUt2v9uJQNzWqSfzTJ9UKLUDsvxHFMEEdrfgq'


def load_state():
    if os.path.exists(STATE):
        try: return json.load(open(STATE))
        except: return {}
    return {}


def save_state(s):
    tmp = STATE + '.new'
    with open(tmp, 'w') as f: json.dump(s, f, indent=2)
    os.replace(tmp, STATE)


def alert(msg):
    """Emit a chat notification line. Prefix with timestamp + emoji-free tag."""
    ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
    print(f'[{ts}] ALERT  {msg}', flush=True)


def gh_repo_head(repo):
    """Return latest commit SHA on the default branch via gh CLI."""
    try:
        out = subprocess.run(['gh', 'api', f'repos/{repo}/commits?per_page=1'],
                             capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout)
        if isinstance(data, list) and data:
            return {
                'sha': data[0]['sha'],
                'date': data[0]['commit']['author']['date'],
                'msg': (data[0]['commit']['message'] or '').split('\n')[0][:100],
                'author': data[0]['commit']['author']['name'],
            }
    except Exception: pass
    return None


def npm_version(pkg):
    """Return (latest version, publish_date) for an npm package."""
    try:
        url = f'https://registry.npmjs.org/{pkg}'
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)
        latest = d.get('dist-tags', {}).get('latest')
        pub = d.get('time', {}).get(latest)
        return {'version': latest, 'published': pub}
    except Exception: return None


def http_status(url):
    """Return HTTP status code + content hash (first 4KB).

    Uses curl subprocess because the local Python's urllib often hits SSL
    cert-verification failures (no certifi installed). Curl honors the system
    trust store and gives us reliable status codes for live-monitoring.
    """
    try:
        # -s silent, -S show error, -L follow redirects, -o body, -w write status
        # Note: do NOT pass -f / --fail, we want non-2xx codes (503!) as status, not error.
        body_path = '/tmp/.solstice_watcher_http'
        out = subprocess.run(
            ['curl', '-sSL', '--max-time', '12',
             '-o', body_path, '-w', '%{http_code}',
             '-H', 'User-Agent: SolsticeWatcher/1.0', url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15,
        )
        code_str = out.stdout.decode().strip()
        code = int(code_str) if code_str.isdigit() else 0
        try:
            with open(body_path, 'rb') as f: data = f.read(4096)
            h = hashlib.sha256(data).hexdigest()[:12]
        except Exception:
            h = None
        return {'status': code, 'hash': h}
    except Exception:
        return {'status': 0, 'hash': None}


DOCS_SITEMAP = 'https://docs.solstice.finance/sitemap-pages.xml'


def docs_pages():
    """Fetch the Solstice docs sitemap and return {url: lastmod_iso}.

    Hashing the docs homepage doesn't detect content edits — the page is a SPA
    shell. The sitemap exposes per-page lastmod timestamps that move whenever
    GitBook saves a page, so it catches both new pages and edits to existing ones.

    Uses curl since this machine's Python urllib lacks valid SSL trust roots
    (failures previously surfaced as silent 0-status state rows).

    Returns {} on any fetch/parse error so the diff is a no-op.
    """
    import re
    try:
        body = subprocess.check_output(
            ['curl', '-sSL', '--max-time', '15',
             '-H', 'User-Agent: SolsticeWatcher/1.0', DOCS_SITEMAP],
            stderr=subprocess.DEVNULL,
        ).decode('utf-8', errors='replace')
    except Exception:
        return {}
    out = {}
    for block in re.findall(r'<url\b[^>]*>(.*?)</url>', body, re.DOTALL):
        m_loc = re.search(r'<loc>([^<]+)</loc>', block)
        m_mod = re.search(r'<lastmod>([^<]+)</lastmod>', block)
        if m_loc and m_mod:
            out[m_loc.group(1).strip()] = m_mod.group(1).strip()
    return out


def latest_sig(addr):
    """Return the most-recent (slot, ts, sig) for an address; None if no sigs."""
    try:
        r = rpc('getSignaturesForAddress', [addr, {'limit': 1}], timeout=10)
        sigs = r.get('result', []) or []
        if sigs:
            s = sigs[0]
            return {'sig': s['signature'], 'slot': s.get('slot'), 'ts': s.get('blockTime')}
    except Exception: pass
    return None


def token_balance(ata):
    try:
        r = rpc('getTokenAccountBalance', [ata], timeout=10)
        info = r.get('result', {}).get('value', {})
        return float(info.get('uiAmount') or 0)
    except Exception: return None


def slx_supply():
    try:
        r = rpc('getAccountInfo', [SLX_MINT, {'encoding':'jsonParsed'}], timeout=10)
        info = r['result']['value']['data']['parsed']['info']
        return float(info['supply']) / (10 ** int(info['decimals']))
    except Exception: return None


def tick(state, first_run):
    """One polling pass. Updates state in-place, emits alerts on changes."""
    # 1. GitHub
    gh = state.setdefault('github', {})
    for repo in GH_REPOS:
        head = gh_repo_head(repo)
        if head is None: continue
        prev = gh.get(repo)
        if prev is None and not first_run:
            alert(f'GH {repo}: first observation — head={head["sha"][:8]} "{head["msg"]}"')
        elif prev and prev.get('sha') != head['sha']:
            alert(f'GH {repo}: NEW COMMIT by {head["author"]} @ {head["date"][:16]} — "{head["msg"]}"')
        gh[repo] = head

    # 2. npm
    npm = state.setdefault('npm', {})
    for pkg in NPM_PACKAGES:
        v = npm_version(pkg)
        if v is None: continue
        prev = npm.get(pkg)
        if prev and prev.get('version') != v['version']:
            major_jump = False
            try:
                prev_major = int(prev['version'].split('.')[0])
                new_major = int(v['version'].split('.')[0])
                major_jump = new_major > prev_major
            except: pass
            prefix = 'MAJOR BUMP' if major_jump else 'bump'
            alert(f'npm {pkg}: {prefix} {prev["version"]} -> {v["version"]} ({v["published"][:16]})')
        npm[pkg] = v

    # 3. HTTP endpoints
    http = state.setdefault('http', {})
    for url in HTTP_ENDPOINTS:
        s = http_status(url)
        prev = http.get(url)
        if prev:
            # Status change
            if prev.get('status') != s.get('status'):
                old_st = prev.get('status'); new_st = s.get('status')
                # 503 -> 200 is the big claim-site flip we care about
                if old_st in (503, 502, 504, 0) and new_st == 200:
                    alert(f'HTTP {url}: STATUS WENT LIVE ({old_st} -> 200)')
                else:
                    alert(f'HTTP {url}: status {old_st} -> {new_st}')
            # Content hash change (for metadata.json)
            elif prev.get('hash') != s.get('hash') and 'metadata.json' in url:
                alert(f'HTTP {url}: content changed (hash {prev["hash"]} -> {s["hash"]})')
        http[url] = s

    # 3b. Solstice docs sitemap (per-page lastmod diff)
    docs = state.setdefault('docs', {})
    pages = docs_pages()
    if pages:  # only diff when fetch succeeded — avoid false alerts on transient failure
        for url, mod in pages.items():
            prev = docs.get(url)
            if prev is None:
                if not first_run:
                    alert(f'docs NEW PAGE: {url}  (lastmod {mod[:16]})')
            elif prev != mod:
                alert(f'docs UPDATED: {url}  ({prev[:16]} -> {mod[:16]})')
        for url in list(docs.keys()):
            if url not in pages:
                if not first_run:
                    alert(f'docs REMOVED: {url}')
                docs.pop(url, None)
        docs.update(pages)

    # 4. On-chain wallet/program activity
    chain = state.setdefault('onchain_sigs', {})
    for label, addr in ONCHAIN_WALLETS.items():
        s = latest_sig(addr)
        if s is None: continue
        prev = chain.get(label)
        if prev and prev.get('sig') != s['sig']:
            ts_str = datetime.fromtimestamp(s.get('ts') or 0, UTC).strftime('%Y-%m-%d %H:%M')
            alert(f'CHAIN {label} ({addr[:8]}..): NEW SIG @ {ts_str} — {s["sig"][:24]}..')
        chain[label] = s

    # 5. SLX treasury balance + supply
    bal = token_balance(SLX_TREASURY_ATA)
    sup = slx_supply()
    prev_bal = state.get('slx_treasury_balance')
    prev_sup = state.get('slx_supply')
    if bal is not None:
        if prev_bal is not None and abs(bal - prev_bal) > 0.001:
            alert(f'SLX TREASURY: balance {prev_bal:,.4f} -> {bal:,.4f} (Δ {bal-prev_bal:+,.4f})')
        state['slx_treasury_balance'] = bal
    if sup is not None:
        if prev_sup is not None and abs(sup - prev_sup) > 0.001:
            alert(f'SLX SUPPLY: {prev_sup:,.4f} -> {sup:,.4f} (Δ {sup-prev_sup:+,.4f})')
        state['slx_supply'] = sup

    state['last_tick'] = datetime.now(UTC).isoformat()


def main():
    print(f'[{datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}] solstice_watcher starting (poll every {POLL_INTERVAL}s)', flush=True)
    state = load_state()
    first_run = ('last_tick' not in state)
    if first_run:
        print(f'[{datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}] first run — establishing baseline (no alerts this tick)', flush=True)
    while True:
        try:
            tick(state, first_run)
            save_state(state)
            first_run = False
        except Exception as e:
            print(f'[{datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}] tick error: {e}', flush=True)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
