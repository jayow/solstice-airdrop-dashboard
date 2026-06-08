"""Private SLX monitoring daemon — NOT for public dashboards.

Polls 6 signal sources and sends Discord webhook alerts when thresholds
are crossed. State lives in data/solstice.db under private_ tables;
nothing renders to server/data.json or any dashboard.

Run: nohup python3 tools/private_alerter.py > /tmp/slx_sentinel.log 2>&1 &

Tunable knobs at the top. Discord webhook URL read from .env.
"""
import json, os, ssl, sqlite3, sys, time, urllib.request
from collections import defaultdict
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, 'data', 'solstice.db')

ENV  = {}
for line in open(os.path.join(ROOT, '.env')):
    if '=' in line and not line.lstrip().startswith('#'):
        k, v = line.strip().split('=', 1)
        ENV[k.strip()] = v.strip()

WEBHOOK    = ENV['DISCORD_WEBHOOK_URL']
HELIUS     = ENV['HELIUS_API_KEY']
BSC_RPC    = ENV['bscrpc']

SLX_MINT_SOL = 'SLXdx4BUt2v9uJQNzWqSfzTJ9UKLUDsvxHFMEEdrfgq'
SLX_BSC      = '0x02BCC4C181B83A8C0A342BC003389CBECB4BC54D'
SLX_ALPHA_ID = '8BFC8D7403DD2C144DBF0B15D627EC6A'

# Key BSC addresses
BSC_AGGREGATOR = '0xb300000b72deaeb607a12d5f54773d1c19c7028d'   # the May 31 dump router
BSC_NULL       = '0x0000000000000000000000000000000000000000'

# Cadences (seconds)
CAD_AGGREGATOR =  5 * 60   #  5 min — leading indicator
CAD_ALPHA      = 15 * 60
CAD_BRIDGE     = 15 * 60
CAD_PERPS      = 15 * 60
CAD_SQUARE     = 15 * 60
CAD_COHORT     = 15 * 60
CAD_PRICE_MOVE =  5 * 60   # Direct price-move detector — fast catch-all (any mechanism)

# Alert thresholds
AGG_MULT_THRESHOLD   = 3.0    # 1h inflow > 3× baseline
AGG_PRICE_GATE_DROP  = -2.0   # ≤ -2% 1h to fire dump alert
AGG_PRICE_GATE_PUMP  = +2.0   # ≥ +2% 1h to fire pump alert
BRIDGE_FLOW_THRESHOLD = 200_000   # 1h net > 200K SLX in either direction
# Perps — desensitized 2026-06-01: small venues swing wildly, was spamming.
PERP_MIN_VENUE_OI       = 200_000   # ignore venues smaller than this for alerts
PERP_OI_DELTA_THRESHOLD = 0.25      # 25% OI swing in 1h (was 10%)
PERP_FUNDING_FLIP_MIN   = 0.015     # 1.5% funding change AND sign flip (was 0.5%)
PERP_MIN_ALERT_VENUES   = 2         # require ≥2 venues to flip in same cycle (consensus)
PERP_DEDUPE_SEC         = 4 * 3600  # 4h dedupe per signal (was 30 min)
COHORT_FLOW_THRESHOLD = 2.0      # daily flow > 2× 7-day baseline
# Direct price-move detector — added 2026-06-01 after missing the +57% pump.
# Catch-all that fires regardless of mechanism (aggregator, perps, etc).
PRICE_MOVE_THRESHOLD_5M  =  3.0   # ±3% in 5 min
PRICE_MOVE_THRESHOLD_15M =  5.0   # ±5% in 15 min
PRICE_MOVE_THRESHOLD_1H  = 10.0   # ±10% in 1h
PRICE_MOVE_DEDUPE_SEC    = 30 * 60   # 30 min per direction (separate dedupe per up/down)
# Alpha volume/holder spike — leading indicator for Alpha-driven moves
ALPHA_VOL_SPIKE_MULT     = 1.5    # vol24h grows 50%+ over a 4h window
ALPHA_HOLDERS_SPIKE_MULT = 1.05   # holders grow 5%+ over a 1h window (Alpha campaign onboarding)
# LP depth change detector — LPs adding/pulling
LP_DEPTH_DELTA_THRESHOLD = 0.30   # ±30% change in BSC LP USD value over 1h

# SSL context (suppress mac cert noise)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA_HEADER = "Mozilla/5.0 (Macintosh) SLXSentinel/1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Discord webhook helper
# ─────────────────────────────────────────────────────────────────────────────

def discord_send(title: str, description: str, color: int, fields: list = None) -> bool:
    """color: 0xFF4444 red (dump), 0x44FF44 green (pump), 0xFFD700 yellow (regime), 0x4499FF blue (info)"""
    payload = {
        "username": "SLX Sentinel",
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
            "footer": {"text": "Solstice private alpha alerter"},
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
        }],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode('utf-8'),
        headers={"Content-Type": "application/json", "User-Agent": UA_HEADER},
    )
    try:
        urllib.request.urlopen(req, timeout=15, context=CTX)
        return True
    except Exception as e:
        log(f'discord_send error: {e}')
        return False


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


_active_connections = []


def db_conn():
    """Caller doesn't need to close — the main loop calls _close_active_connections()
    after each module run. Without this, the SQLite WAL reader-mark stayed pinned
    across the 30s polling sleep, growing the WAL to 10 GB."""
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    _active_connections.append(con)
    return con


def _close_active_connections():
    """Release every connection opened since the last close. Safe to call
    repeatedly. Stops the WAL reader-mark from being pinned by stale daemon
    connections across the 30s polling sleep."""
    while _active_connections:
        c = _active_connections.pop()
        try:
            c.close()
        except Exception:
            pass


def already_fired(con, signal_key: str, dedupe_window_sec: int) -> bool:
    """Don't double-fire the same signal within dedupe_window."""
    now = int(time.time())
    r = con.execute(
        "SELECT MAX(ts) FROM private_alerts_fired WHERE signal_key=?",
        (signal_key,),
    ).fetchone()
    last = r[0] if r and r[0] else 0
    return (now - last) < dedupe_window_sec


def record_fired(con, signal_key: str, severity: str, payload: dict):
    con.execute(
        "INSERT OR REPLACE INTO private_alerts_fired (ts, signal_key, severity, payload_json) VALUES (?,?,?,?)",
        (int(time.time()), signal_key, severity, json.dumps(payload)),
    )
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# RPC helpers
# ─────────────────────────────────────────────────────────────────────────────

def bsc_rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(BSC_RPC, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())


def http_get_json(url: str, headers: dict = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_HEADER})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())


# ─────────────────────────────────────────────────────────────────────────────
# Module 1 — BSC aggregator inflow (5 min, leading indicator)
# ─────────────────────────────────────────────────────────────────────────────

_CONTRACT_TYPE_CACHE = {}   # addr → bool (is_contract). Persists for process lifetime.

def _is_contract(addr: str) -> bool:
    """Cache eth_getCode results — sender set is small and stable across polls."""
    addr = addr.lower()
    if addr in _CONTRACT_TYPE_CACHE:
        return _CONTRACT_TYPE_CACHE[addr]
    try:
        code = bsc_rpc('eth_getCode', [addr, 'latest']).get('result', '0x')
        is_c = len(code) > 4
    except Exception:
        is_c = False   # assume EOA on error so we don't suppress legitimate alerts
    _CONTRACT_TYPE_CACHE[addr] = is_c
    return is_c


def _get_bsc_price_1h_change():
    """Pull current BSC SLX price + 1h change % via DexScreener. Returns (price, change_1h_pct) or (None, None) on error."""
    try:
        j = http_get_json(
            'https://api.dexscreener.com/latest/dex/tokens/' + SLX_BSC,
            headers={'User-Agent': UA_HEADER},
        )
        pairs = sorted(j.get('pairs', []), key=lambda x: -(x.get('volume', {}).get('h24') or 0))
        if not pairs:
            return None, None
        p = pairs[0]
        return float(p.get('priceUsd') or 0), float(p.get('priceChange', {}).get('h1') or 0)
    except Exception as e:
        log(f'_get_bsc_price_1h_change error: {e}')
        return None, None


def poll_bsc_aggregator():
    """Watch 0xb300000b inflows. Patched 2026-06-01:
    - Distinguish EOA inflows (real sells) from contract-to-contract routing (noise)
    - Require price gate: 1h price drop ≥ 2% OR (EOA inflow alone exceeds threshold)
    """
    try:
        bn = int(bsc_rpc('eth_blockNumber', [])['result'], 16)
        bn_1h = bn - 1200

        all_in = []
        page = None
        for _ in range(10):
            params = {
                "contractAddresses": [SLX_BSC], "category": ["erc20"],
                "fromBlock": hex(bn_1h), "toBlock": hex(bn),
                "toAddress": BSC_AGGREGATOR, "maxCount": "0x3e8",
            }
            if page:
                params['pageKey'] = page
            r = bsc_rpc('alchemy_getAssetTransfers', [params])
            res = r.get('result', {})
            all_in.extend(res.get('transfers', []))
            page = res.get('pageKey')
            if not page:
                break

        # Split inflow by sender type
        from collections import defaultdict as _dd
        sender_totals = _dd(float)
        for t in all_in:
            sender_totals[t['from'].lower()] += float(t.get('value', 0) or 0)

        eoa_inflow = 0.0
        contract_inflow = 0.0
        eoa_senders = set()
        for addr, v in sender_totals.items():
            if _is_contract(addr):
                contract_inflow += v
            else:
                eoa_inflow += v
                eoa_senders.add(addr)

        total_inflow = eoa_inflow + contract_inflow
        now = int(time.time())

        con = db_conn()
        con.execute(
            "INSERT OR REPLACE INTO private_bsc_aggregator_snapshots (ts, aggregator, inflow_1h_slx, outflow_1h_slx, net_1h_slx, unique_senders_1h) VALUES (?,?,?,?,?,?)",
            (now, BSC_AGGREGATOR, eoa_inflow, contract_inflow, total_inflow, len(eoa_senders)),
        )

        # Baseline: 7-day rolling avg of EOA-only inflow (filtered metric — apples-to-apples)
        baseline_row = con.execute(
            "SELECT AVG(inflow_1h_slx) FROM private_bsc_aggregator_snapshots WHERE aggregator=? AND ts >= ?",
            (BSC_AGGREGATOR, now - 7 * 86400),
        ).fetchone()
        baseline = baseline_row[0] if baseline_row and baseline_row[0] else 0
        con.commit()

        # Price gate — only fire if BSC pool price has actually moved
        price_now, price_change_1h = _get_bsc_price_1h_change()

        # Two-condition trigger: (a) EOA inflow > 3× baseline AND (b) price dropped ≥ 2% in 1h
        eoa_spike = baseline > 1000 and eoa_inflow > AGG_MULT_THRESHOLD * baseline
        price_moved = price_change_1h is not None and price_change_1h <= -2.0

        if eoa_spike and price_moved and not already_fired(con, 'agg_inflow_spike', 3600):
            mult = eoa_inflow / baseline if baseline else 0
            est_usd = eoa_inflow * get_slx_price_cached()
            discord_send(
                title="🔴 BSC AGGREGATOR EOA SELL WAVE",
                description=f"Real EOA sells through `0xb300000b…` exceeded baseline by **{mult:.1f}×**, AND BSC pool price dropped **{price_change_1h:.1f}%** in 1h.\nLikely continued downside in next 30-60 min as more flow routes.",
                color=0xFF4444,
                fields=[
                    {"name": "EOA 1h inflow", "value": f"{eoa_inflow:>10,.0f} SLX (~${est_usd:,.0f})", "inline": True},
                    {"name": "7d EOA baseline / h", "value": f"{baseline:>10,.0f} SLX", "inline": True},
                    {"name": "Multiplier", "value": f"{mult:.1f}×", "inline": True},
                    {"name": "Unique EOA senders", "value": str(len(eoa_senders)), "inline": True},
                    {"name": "BSC price 1h", "value": f"{price_change_1h:+.2f}%", "inline": True},
                    {"name": "Contract routing (excluded)", "value": f"{contract_inflow:>10,.0f} SLX", "inline": True},
                ],
            )
            record_fired(con, 'agg_inflow_spike', 'high', {"eoa_inflow": eoa_inflow, "baseline": baseline, "mult": mult, "price_change_1h": price_change_1h})

        # Now check the BUY side — was there a buy wave routed OUT of the aggregator?
        all_out = []
        page = None
        for _ in range(10):
            params = {
                "contractAddresses": [SLX_BSC], "category": ["erc20"],
                "fromBlock": hex(bn_1h), "toBlock": hex(bn),
                "fromAddress": BSC_AGGREGATOR, "maxCount": "0x3e8",
            }
            if page:
                params['pageKey'] = page
            r = bsc_rpc('alchemy_getAssetTransfers', [params])
            res = r.get('result', {})
            all_out.extend(res.get('transfers', []))
            page = res.get('pageKey')
            if not page:
                break

        recipient_totals = _dd(float)
        for t in all_out:
            recipient_totals[t['to'].lower()] += float(t.get('value', 0) or 0)
        eoa_buyer_inflow = 0.0
        eoa_buyers = set()
        for addr, v in recipient_totals.items():
            if not _is_contract(addr):
                eoa_buyer_inflow += v
                eoa_buyers.add(addr)

        price_pumped = price_change_1h is not None and price_change_1h >= AGG_PRICE_GATE_PUMP
        eoa_buy_spike = baseline > 1000 and eoa_buyer_inflow > AGG_MULT_THRESHOLD * baseline

        if eoa_buy_spike and price_pumped and not already_fired(con, 'agg_buy_wave_pump', 3600):
            mult = eoa_buyer_inflow / baseline if baseline else 0
            est_usd = eoa_buyer_inflow * (price_now or get_slx_price_cached())
            discord_send(
                title="🟢 BSC AGGREGATOR BUY WAVE — PUMP",
                description=f"Real EOA buys through `0xb300000b…` exceeded baseline by **{mult:.1f}×**, AND BSC pool price rose **+{price_change_1h:.1f}%** in 1h.\nMomentum likely continues in next 30-60 min.",
                color=0x44FF44,
                fields=[
                    {"name": "EOA buyers 1h", "value": f"{eoa_buyer_inflow:>10,.0f} SLX (~${est_usd:,.0f})", "inline": True},
                    {"name": "Multiplier vs baseline", "value": f"{mult:.1f}×", "inline": True},
                    {"name": "BSC price 1h", "value": f"+{price_change_1h:.2f}%", "inline": True},
                    {"name": "Unique buyer wallets", "value": str(len(eoa_buyers)), "inline": True},
                    {"name": "Current price", "value": f"${price_now:.4f}" if price_now else "?", "inline": True},
                ],
            )
            record_fired(con, 'agg_buy_wave_pump', 'high', {"eoa_inflow": eoa_buyer_inflow, "baseline": baseline, "mult": mult, "price_change_1h": price_change_1h})

        # Also: emit a structural HIGH-VOLUME info notice (rare, lower urgency) if EOA spike present
        # but price hasn't moved — means the buy side is absorbing it (could pre-stage a pump if sustained)
        if eoa_spike and not price_moved and not already_fired(con, 'agg_eoa_spike_absorbed', 4 * 3600):
            mult = eoa_inflow / baseline if baseline else 0
            discord_send(
                title="🟡 EOA sells routing but price absorbing",
                description=f"Elevated EOA sells (**{mult:.1f}×** baseline) but BSC price held ({price_change_1h:+.2f}% 1h). Buy side currently winning. Watch for follow-through.",
                color=0xFFD700,
                fields=[
                    {"name": "EOA 1h inflow", "value": f"{eoa_inflow:>10,.0f} SLX", "inline": True},
                    {"name": "BSC price 1h", "value": f"{(price_change_1h or 0):+.2f}%", "inline": True},
                    {"name": "EOA senders", "value": str(len(eoa_senders)), "inline": True},
                ],
            )
            record_fired(con, 'agg_eoa_spike_absorbed', 'low', {"eoa_inflow": eoa_inflow, "mult": mult, "price_change_1h": price_change_1h})

        log(f'aggregator: EOA_1h={eoa_inflow:>9,.0f}  contract_1h={contract_inflow:>10,.0f}  baseline_h={baseline:>9,.0f}  EOA_senders={len(eoa_senders)}  price_1h={price_change_1h}')
    except Exception as e:
        log(f'poll_bsc_aggregator error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Module 2 — Alpha API (15 min, structural regime changes)
# ─────────────────────────────────────────────────────────────────────────────

_SLX_PRICE_CACHE = {'price': 0.19, 'ts': 0}

def get_slx_price_cached() -> float:
    """Use latest Alpha snapshot price (refreshed every 15 min)."""
    if time.time() - _SLX_PRICE_CACHE['ts'] < 900:
        return _SLX_PRICE_CACHE['price']
    return _SLX_PRICE_CACHE['price']


def poll_alpha_api():
    """Watch mulPoint, onlineAirdrop, fullyDelisted + holders/volume/price."""
    try:
        url = 'https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list'
        j = http_get_json(url, headers={'User-Agent': UA_HEADER})
        tokens = j.get('data', [])
        slx = next((t for t in tokens if t.get('tokenId') == SLX_ALPHA_ID), None)
        if not slx:
            log('alpha: SLX missing from Alpha list!')
            con = db_conn()
            if not already_fired(con, 'alpha_missing', 3600):
                discord_send(
                    title="🔴 SLX REMOVED FROM BINANCE ALPHA",
                    description="Solstice token NOT FOUND in Binance Alpha list. Possible delisting.",
                    color=0xFF0000,
                )
                record_fired(con, 'alpha_missing', 'critical', {})
            return

        now = int(time.time())
        mul_point      = int(slx.get('mulPoint') or 0)
        online_airdrop = int(bool(slx.get('onlineAirdrop')))
        online_tge     = int(bool(slx.get('onlineTge')))
        listing_cex    = int(bool(slx.get('listingCex')))
        fully_delisted = int(bool(slx.get('fullyDelisted')))
        score          = int(slx.get('score') or 0)
        holders        = int(slx.get('holders') or 0)
        price          = float(slx.get('price') or 0)
        volume_24h     = float(slx.get('volume24h') or 0)
        liquidity      = float(slx.get('liquidity') or 0)
        count_24h      = int(slx.get('count24h') or 0)

        _SLX_PRICE_CACHE['price'] = price
        _SLX_PRICE_CACHE['ts']    = now

        con = db_conn()
        # Compare to last snapshot
        last = con.execute(
            "SELECT mul_point, online_airdrop, listing_cex, fully_delisted, score, holders FROM private_alpha_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        con.execute(
            "INSERT OR REPLACE INTO private_alpha_snapshots (ts, mul_point, online_airdrop, online_tge, listing_cex, fully_delisted, score, holders, price_usd, volume_24h_usd, liquidity_usd, count_24h, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, mul_point, online_airdrop, online_tge, listing_cex, fully_delisted, score, holders, price, volume_24h, liquidity, count_24h, json.dumps(slx)),
        )
        con.commit()

        if last:
            # Detect regime changes
            if mul_point != last['mul_point']:
                direction = "🟢 UP" if mul_point > last['mul_point'] else "🔴 DOWN"
                color = 0x44FF44 if mul_point > last['mul_point'] else 0xFF4444
                discord_send(
                    title=f"⚡ Alpha multiplier change {direction}",
                    description=f"`mulPoint`: **{last['mul_point']}× → {mul_point}×**\n\nThis directly controls Alpha Points farmers' yield on SLX trades. Volume/price impact likely in next 1-4h.",
                    color=color,
                    fields=[
                        {"name": "Was", "value": f"{last['mul_point']}×", "inline": True},
                        {"name": "Now", "value": f"{mul_point}×", "inline": True},
                        {"name": "Direction", "value": direction, "inline": True},
                    ],
                )
                record_fired(con, 'alpha_mulpoint_change', 'high', {"from": last['mul_point'], "to": mul_point})

            if fully_delisted and not last['fully_delisted']:
                discord_send(
                    title="🔴🔴🔴 SLX FULLY DELISTED FROM ALPHA",
                    description="`fullyDelisted` flipped to true. Expect immediate mass exit.",
                    color=0xFF0000,
                )
                record_fired(con, 'alpha_delisted', 'critical', {})

            if online_airdrop != last['online_airdrop']:
                msg = "ENABLED" if online_airdrop else "DISABLED"
                color = 0x44FF44 if online_airdrop else 0xFF8844
                discord_send(
                    title=f"🟡 Alpha airdrop flag {msg}",
                    description=f"`onlineAirdrop`: {bool(last['online_airdrop'])} → {bool(online_airdrop)}",
                    color=color,
                )
                record_fired(con, 'alpha_airdrop_flag_change', 'medium', {"from": last['online_airdrop'], "to": online_airdrop})

            if listing_cex and not last['listing_cex']:
                discord_send(
                    title="🟢🟢🟢 SLX LISTED ON BINANCE SPOT",
                    description="`listingCex` flipped TRUE. Binance Spot listing live or imminent.",
                    color=0x00FF00,
                )
                record_fired(con, 'alpha_cex_listing', 'critical', {})

        # Volume/holder spike checks (leading-indicator alerts)
        _check_alpha_spikes(con, now, slx)

        log(f'alpha: price=${price:.4f} mul={mul_point}× airdrop={bool(online_airdrop)} delisted={bool(fully_delisted)} holders={holders:,} vol24h=${volume_24h:,.0f}')
    except Exception as e:
        log(f'poll_alpha_api error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Module 3 — CCIP bridge mint/burn (15 min)
# ─────────────────────────────────────────────────────────────────────────────

def poll_bridge_flow():
    try:
        bn = int(bsc_rpc('eth_blockNumber', [])['result'], 16)
        bn_1h = bn - 1200

        mints, burns = [], []
        for direction, addr_filter in [('mint', 'fromAddress'), ('burn', 'toAddress')]:
            page = None
            for _ in range(5):
                params = {
                    "contractAddresses": [SLX_BSC], "category": ["erc20"],
                    "fromBlock": hex(bn_1h), "toBlock": hex(bn),
                    addr_filter: BSC_NULL, "maxCount": "0x3e8",
                }
                if page:
                    params['pageKey'] = page
                r = bsc_rpc('alchemy_getAssetTransfers', [params])
                res = r.get('result', {})
                items = res.get('transfers', [])
                (mints if direction == 'mint' else burns).extend(items)
                page = res.get('pageKey')
                if not page:
                    break

        minted = sum(float(t.get('value', 0) or 0) for t in mints)
        burned = sum(float(t.get('value', 0) or 0) for t in burns)
        net    = minted - burned
        now    = int(time.time())

        con = db_conn()
        con.execute(
            "INSERT OR REPLACE INTO private_bridge_snapshots (ts, minted_1h_slx, burned_1h_slx, net_flow_1h_slx, unique_mint_dests_1h, unique_burn_srcs_1h) VALUES (?,?,?,?,?,?)",
            (now, minted, burned, net, len(set(t['to'] for t in mints)), len(set(t['from'] for t in burns))),
        )
        con.commit()

        if abs(net) > BRIDGE_FLOW_THRESHOLD and not already_fired(con, 'bridge_flow_spike', 3600):
            direction = "FROM Solana → BSC (potential sellers loading)" if net > 0 else "FROM BSC → Solana (supply leaving BSC)"
            color = 0xFF4444 if net > 0 else 0x4499FF
            discord_send(
                title="🟡 Cross-chain bridge net flow spike",
                description=f"1h net SLX bridge flow: **{net:+,.0f} SLX** ({direction})",
                color=color,
                fields=[
                    {"name": "Minted on BSC 1h", "value": f"{minted:,.0f} SLX", "inline": True},
                    {"name": "Burned on BSC 1h", "value": f"{burned:,.0f} SLX", "inline": True},
                    {"name": "Net", "value": f"{net:+,.0f} SLX", "inline": True},
                ],
            )
            record_fired(con, 'bridge_flow_spike', 'medium', {"minted": minted, "burned": burned, "net": net})

        log(f'bridge: mint={minted:>10,.0f} burn={burned:>10,.0f} net={net:+,.0f}')
    except Exception as e:
        log(f'poll_bridge_flow error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Module 4 — Perp OI + funding (15 min)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_venue_funding_direct():
    """Query each major perp venue's NATIVE API for SLX funding + cycle interval.
    Returns list of dicts: {venue, rate, interval_sec, daily_cost_pct, mark_price, cap_hit}.
    More accurate than CoinGecko's aggregate (which we discovered was misleading).
    """
    out = []
    def _fetch(url, headers=None):
        try:
            req = urllib.request.Request(url, headers=headers or {'User-Agent': UA_HEADER, 'Accept': 'application/json'})
            return json.loads(urllib.request.urlopen(req, timeout=10, context=CTX).read())
        except Exception:
            return None

    # Gate (4h cycle, -2% cap)
    j = _fetch('https://api.gateio.ws/api/v4/futures/usdt/contracts/SLX_USDT')
    if j and 'funding_rate' in j:
        rate = float(j['funding_rate']); interval = int(j.get('funding_interval', 14400))
        out.append({'venue':'Gate','rate':rate,'interval':interval,'daily':rate*(86400/interval)*100,
                    'mark':float(j.get('mark_price') or 0),'cap_hit':abs(rate)>=0.02-1e-9})
    # BingX (8h)
    j = _fetch('https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol=SLX-USDT')
    if j and j.get('data'):
        d = j['data'] if isinstance(j['data'], dict) else j['data'][0]
        rate = float(d.get('lastFundingRate', 0))
        out.append({'venue':'BingX','rate':rate,'interval':28800,'daily':rate*3*100,
                    'mark':float(d.get('markPrice') or 0),'cap_hit':abs(rate)>=0.015})
    # MEXC (8h)
    j = _fetch('https://contract.mexc.com/api/v1/contract/funding_rate/SLX_USDT')
    if j and j.get('data'):
        d = j['data']; rate = float(d.get('fundingRate', 0))
        out.append({'venue':'MEXC','rate':rate,'interval':28800,'daily':rate*3*100,
                    'mark':float(d.get('lastPrice') or 0),'cap_hit':abs(rate)>=0.015})
    # Bitget (8h)
    j = _fetch('https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol=SLXUSDT&productType=USDT-FUTURES')
    if j and j.get('data'):
        d = j['data'][0] if isinstance(j['data'], list) else j['data']
        rate = float(d.get('fundingRate', 0))
        out.append({'venue':'Bitget','rate':rate,'interval':28800,'daily':rate*3*100,
                    'mark':0,'cap_hit':abs(rate)>=0.015})
    # BitMart (8h)
    j = _fetch('https://api-cloud-v2.bitmart.com/contract/public/details?symbol=SLXUSDT')
    if j and j.get('data'):
        syms = (j['data'].get('symbols') or [])
        if syms:
            d = syms[0]; rate = float(d.get('funding_rate', 0))
            out.append({'venue':'BitMart','rate':rate,'interval':28800,'daily':rate*3*100,
                        'mark':float(d.get('last_price') or 0),'cap_hit':abs(rate)>=0.015})
    # Bybit (8h)
    j = _fetch('https://api.bybit.com/v5/market/tickers?category=linear&symbol=SLXUSDT')
    if j and (j.get('result') or {}).get('list'):
        d = j['result']['list'][0]
        rate = float(d.get('fundingRate', 0))
        out.append({'venue':'Bybit','rate':rate,'interval':28800,'daily':rate*3*100,
                    'mark':float(d.get('markPrice') or 0),'cap_hit':abs(rate)>=0.015})
    # OrangeX (Deribit-style)
    j = _fetch('https://api.orangex.com/api/v1/public/get_funding_rate?instrument_name=SLX-USDT-PERPETUAL')
    if j and j.get('result'):
        r = j['result']
        if isinstance(r, dict):
            rate = float(r.get('current_funding', 0) or r.get('funding_8h', 0) or 0)
            out.append({'venue':'OrangeX','rate':rate,'interval':28800,'daily':rate*3*100,
                        'mark':0,'cap_hit':abs(rate)>=0.015})
    return out


def poll_perps():
    try:
        # CoinGecko sometimes slow — retry once on timeout
        try:
            j = http_get_json('https://api.coingecko.com/api/v3/derivatives', headers={'User-Agent': UA_HEADER})
        except (URLError, HTTPError):
            time.sleep(3)
            j = http_get_json('https://api.coingecko.com/api/v3/derivatives', headers={'User-Agent': UA_HEADER})
        slx = [d for d in j if 'SLX' in (d.get('symbol') or '').upper()]
        now = int(time.time())

        con = db_conn()
        # Snapshot every venue
        for d in slx:
            venue = d.get('market', '?')
            oi = float(d.get('open_interest') or 0)
            f  = float(d.get('funding_rate') or 0)
            v  = float(d.get('volume_24h') or 0)
            p  = float(d.get('price') or 0)
            con.execute(
                "INSERT OR REPLACE INTO private_perp_snapshots (ts, venue, oi_usd, funding_rate, volume_24h_usd, price) VALUES (?,?,?,?,?,?)",
                (now, venue, oi, f, v, p),
            )

        # Detect regime changes by comparing each venue to its 1h-ago snapshot.
        # Desensitized 2026-06-01: only meaningfully-sized venues, bigger thresholds,
        # require consensus (multiple venues flip together) before alerting.
        oi_alerts = []
        funding_alerts = []
        for d in slx:
            venue = d.get('market', '?')
            oi_now = float(d.get('open_interest') or 0)
            f_now  = float(d.get('funding_rate') or 0)
            if oi_now < PERP_MIN_VENUE_OI:
                continue   # skip small/noisy venues
            prev = con.execute(
                "SELECT oi_usd, funding_rate FROM private_perp_snapshots WHERE venue=? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (venue, now - 3300),  # ~55 min ago
            ).fetchone()
            if not prev or not prev['oi_usd'] or prev['oi_usd'] < PERP_MIN_VENUE_OI:
                continue
            d_oi = (oi_now - prev['oi_usd']) / prev['oi_usd']
            d_f  = f_now - prev['funding_rate']
            if abs(d_oi) > PERP_OI_DELTA_THRESHOLD:
                oi_alerts.append(f"{venue}: OI {d_oi:+.1%} ({prev['oi_usd']:,.0f} → {oi_now:,.0f})")
            if abs(d_f) > PERP_FUNDING_FLIP_MIN and ((prev['funding_rate'] >= 0) != (f_now >= 0)):
                funding_alerts.append(f"{venue}: funding FLIP {prev['funding_rate']:+.4f} → {f_now:+.4f}")

        con.commit()

        # Only alert when ≥N venues confirm the regime change (consensus filter)
        all_alerts = oi_alerts + funding_alerts
        total_signals = len(oi_alerts) + len(funding_alerts)
        if total_signals >= PERP_MIN_ALERT_VENUES and not already_fired(con, 'perp_regime', PERP_DEDUPE_SEC):
            discord_send(
                title="🟡 Perp OI / funding regime change",
                description=f"Consensus signal across {total_signals} venues:\n\n" + "\n".join(all_alerts[:8]),
                color=0xFFD700,
            )
            record_fired(con, 'perp_regime', 'medium', {"alerts": all_alerts})

        # ── NEW: Direct-from-venue funding ground truth (added 2026-06-01 after
        # CoinGecko's mangled aggregate misled the Gate -2%-cap read).
        # Pull funding from each venue's native API; fire when ≥2 venues exceed
        # the brutal-funding threshold (≥ 1%/day cost to hold short OR long).
        try:
            direct = _fetch_venue_funding_direct()
            brutal_short = [d for d in direct if d['daily'] <= -1.0]
            brutal_long  = [d for d in direct if d['daily'] >= +1.0]
            cap_hits = [d for d in direct if d['cap_hit']]

            def _fmt_venues(rows):
                return "\n".join(
                    f"• **{d['venue']}**: {d['daily']:+.2f}%/day "
                    f"({d['interval']//3600}h cycle, rate {d['rate']:+.4f}{', CAP' if d['cap_hit'] else ''})"
                    for d in sorted(rows, key=lambda x: x['daily'])
                )

            if len(brutal_short) >= 2 and not already_fired(con, 'perp_short_extreme', 4 * 3600):
                discord_send(
                    title=f"🔴 SHORTS EXTREMELY CROWDED — {len(brutal_short)} venues",
                    description=(
                        f"Shorts paying ≥1%/day on {len(brutal_short)} venues. "
                        f"Squeeze setup loaded.\n\n{_fmt_venues(brutal_short)}\n\n"
                        f"_Per 7d cost (if held): worst venue ≈{brutal_short[0]['daily']*7:.1f}%_"
                    ),
                    color=0xFF4444,
                )
                record_fired(con, 'perp_short_extreme', 'high', {'venues': [d['venue'] for d in brutal_short]})

            if len(brutal_long) >= 2 and not already_fired(con, 'perp_long_extreme', 4 * 3600):
                discord_send(
                    title=f"🟢 LONGS EXTREMELY CROWDED — {len(brutal_long)} venues",
                    description=(
                        f"Longs paying ≥1%/day on {len(brutal_long)} venues. Liquidation cascade risk.\n\n"
                        f"{_fmt_venues(brutal_long)}"
                    ),
                    color=0x44FF44,
                )
                record_fired(con, 'perp_long_extreme', 'high', {'venues': [d['venue'] for d in brutal_long]})

            log(f'perps direct: {len(direct)} venues, brutal_short={len(brutal_short)} brutal_long={len(brutal_long)} caps_hit={len(cap_hits)}')
        except Exception as e:
            log(f'perps direct-fetch error: {e}')

        total_oi = sum(float(d.get('open_interest') or 0) for d in slx)
        log(f'perps: total_OI=${total_oi:,.0f}  venues_with_OI={sum(1 for d in slx if (d.get("open_interest") or 0)>0)}')
    except Exception as e:
        log(f'poll_perps error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Module 5 — Binance Square / CMS catalog 93 (15 min)
# ─────────────────────────────────────────────────────────────────────────────

def poll_square():
    """Catalogs 93 (Latest Activities) + 48 (New Listings) + 161 (Delisting). Watch SLX mentions."""
    try:
        articles = []
        # 48=New Listings (catches Binance Spot listings), 49=Activities, 93=Activities-alt, 161=Delisting/Alpha Removal
        for catalog_id in [48, 49, 93, 161]:
            url = f'https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId={catalog_id}&pageNo=1&pageSize=20'
            try:
                j = http_get_json(url, headers={'User-Agent': UA_HEADER, 'lang': 'en', 'Accept': 'application/json'})
                data = j.get('data') or {}
                items = data.get('articles', []) or (data.get('catalogs') or [{}])[0].get('articles', [])
                for a in items:
                    a['_catalog'] = catalog_id
                articles.extend(items)
            except Exception as e:
                log(f'  square catalog {catalog_id} skipped: {e}')

        con = db_conn()
        new_hits = []
        for a in articles:
            aid = str(a.get('id') or a.get('code') or '')
            title = a.get('title') or ''
            if not aid or not title:
                continue
            # Already seen?
            seen = con.execute("SELECT 1 FROM private_square_articles WHERE article_id=?", (aid,)).fetchone()
            if seen:
                continue
            con.execute(
                "INSERT OR REPLACE INTO private_square_articles (article_id, title, catalog_id, publish_ts, url, notified) VALUES (?,?,?,?,?,?)",
                (aid, title, a.get('_catalog', 93), int(time.time()), f"https://www.binance.com/en/support/announcement/detail/{a.get('code', aid)}", 0),
            )
            # Match SLX or Solstice or Solstice-relevant Alpha Trading Competition keywords
            up = title.upper()
            if 'SOLSTICE' in up or 'SLX' in up.split():
                new_hits.append((aid, title))
        con.commit()

        for aid, title in new_hits:
            discord_send(
                title="🟢 New Binance Square / Alpha announcement",
                description=f"**{title}**\nhttps://www.binance.com/en/support/announcement/detail/{aid}",
                color=0x00FF00,
            )
            con.execute("UPDATE private_square_articles SET notified=1 WHERE article_id=?", (aid,))
        con.commit()
        log(f'square: scanned {len(articles)} articles, {len(new_hits)} new SLX hits')
    except Exception as e:
        log(f'poll_square error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Module 6 — Direct price-move detector (5 min — catch-all, mechanism-agnostic)
# Added 2026-06-01 after the alerter missed the +57% pump.
# ─────────────────────────────────────────────────────────────────────────────

def poll_price_move():
    """Watch BSC pool price + Alpha volume + holders. Fire on any meaningful move
    regardless of which upstream mechanism caused it. This is the safety-net detector."""
    try:
        # 1) Direct price-move from DexScreener (gives us 5m/1h/6h/24h windows for free)
        j = http_get_json('https://api.dexscreener.com/latest/dex/tokens/' + SLX_BSC,
                          headers={'User-Agent': UA_HEADER})
        pairs = sorted(j.get('pairs', []), key=lambda x: -(x.get('volume', {}).get('h24') or 0))
        if not pairs:
            log('price_move: no DexScreener pairs')
            return
        p = pairs[0]
        price = float(p.get('priceUsd') or 0)
        pc = p.get('priceChange', {}) or {}
        c5m  = float(pc.get('m5')  or 0)
        c1h  = float(pc.get('h1')  or 0)
        c6h  = float(pc.get('h6')  or 0)
        c24h = float(pc.get('h24') or 0)
        liq_usd = float((p.get('liquidity', {}) or {}).get('usd') or 0)
        vol_1h = float((p.get('volume', {}) or {}).get('h1') or 0)
        txns_1h = (p.get('txns', {}) or {}).get('h1', {}) or {}
        buys_1h = int(txns_1h.get('buys', 0) or 0)
        sells_1h = int(txns_1h.get('sells', 0) or 0)

        con = db_conn()
        now = int(time.time())

        # 2) Fire on FIRST threshold hit (5m → 15m proxy via h1 ÷ 4 if no 15m field → use 5m × 3 as proxy ; use direct h1 for the 1h gate)
        triggers = []
        if abs(c5m) >= PRICE_MOVE_THRESHOLD_5M:
            triggers.append(('5m', c5m, PRICE_MOVE_THRESHOLD_5M))
        if abs(c1h) >= PRICE_MOVE_THRESHOLD_1H:
            triggers.append(('1h', c1h, PRICE_MOVE_THRESHOLD_1H))

        for window, change, threshold in triggers:
            direction = 'PUMP' if change > 0 else 'DUMP'
            signal_key = f'price_move_{window}_{direction.lower()}'
            if already_fired(con, signal_key, PRICE_MOVE_DEDUPE_SEC):
                continue
            emoji = '🟢' if change > 0 else '🔴'
            color = 0x44FF44 if change > 0 else 0xFF4444
            discord_send(
                title=f"{emoji} SLX {direction} — {window} ±{abs(change):.1f}%",
                description=f"Direct price move detected: **{change:+.2f}% in {window}** on BSC pool. Mechanism-agnostic catch-all alert.",
                color=color,
                fields=[
                    {"name": "Current price", "value": f"${price:.4f}", "inline": True},
                    {"name": f"Change {window}", "value": f"{change:+.2f}%", "inline": True},
                    {"name": "Liquidity", "value": f"${liq_usd:,.0f}", "inline": True},
                    {"name": "1h buys / sells", "value": f"{buys_1h:,} / {sells_1h:,}", "inline": True},
                    {"name": "1h volume", "value": f"${vol_1h:,.0f}", "inline": True},
                    {"name": "Other windows", "value": f"5m {c5m:+.1f}%\n6h {c6h:+.1f}%\n24h {c24h:+.1f}%", "inline": True},
                ],
            )
            record_fired(con, signal_key, 'high', {"window": window, "change_pct": change, "price": price})

        # 3) LP depth delta — compare current liq to last reading
        last_liq = con.execute(
            "SELECT liquidity_usd FROM private_alpha_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
            (now - 1800,),  # ≥ 30 min ago
        ).fetchone()
        if last_liq and last_liq[0] and last_liq[0] > 100_000:
            d_liq = (liq_usd - last_liq[0]) / last_liq[0]
            if abs(d_liq) > LP_DEPTH_DELTA_THRESHOLD and not already_fired(con, 'lp_depth_change', 4 * 3600):
                direction = 'GREW' if d_liq > 0 else 'SHRANK'
                color = 0x44FF44 if d_liq > 0 else 0xFF8844
                emoji = '🟢' if d_liq > 0 else '🟠'
                discord_send(
                    title=f"{emoji} BSC LP depth {direction} {abs(d_liq)*100:.0f}%",
                    description=f"BSC pool LP went from ${last_liq[0]:,.0f} → ${liq_usd:,.0f}. {'More resilience to large orders.' if d_liq > 0 else 'Thinner book — moves will be sharper.'}",
                    color=color,
                    fields=[
                        {"name": "Was", "value": f"${last_liq[0]:,.0f}", "inline": True},
                        {"name": "Now", "value": f"${liq_usd:,.0f}", "inline": True},
                        {"name": "Δ", "value": f"{d_liq*100:+.1f}%", "inline": True},
                    ],
                )
                record_fired(con, 'lp_depth_change', 'medium', {"from": last_liq[0], "to": liq_usd, "pct": d_liq})

        log(f'price_move: ${price:.4f}  5m={c5m:+.2f}% 1h={c1h:+.2f}% 24h={c24h:+.2f}%  LP=${liq_usd:,.0f}')
    except Exception as e:
        log(f'poll_price_move error: {e}')


# Alpha volume + holder spike — leading-indicator detection from inside the Alpha poll
def _check_alpha_spikes(con, now: int, current_snapshot: dict):
    """Detect Alpha vol24h spike or holder count growth — both fire as 🟢 pre-pump signals."""
    # Vol24h spike — compare to 4h-prior snapshot
    prev_vol_row = con.execute(
        "SELECT volume_24h_usd FROM private_alpha_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
        (now - 4 * 3600,),
    ).fetchone()
    if prev_vol_row and prev_vol_row[0] and prev_vol_row[0] > 1_000_000:
        cur_vol = float(current_snapshot.get('volume24h') or 0)
        d = cur_vol / prev_vol_row[0]
        if d >= ALPHA_VOL_SPIKE_MULT and not already_fired(con, 'alpha_vol_spike', 4 * 3600):
            discord_send(
                title="🟢 Alpha vol24h spike",
                description=f"Binance Alpha 24h volume rose **{(d-1)*100:.0f}%** vs 4h ago.\nUsually precedes price moves — Alpha bringing in new traders.",
                color=0x44FF44,
                fields=[
                    {"name": "Was 4h ago", "value": f"${prev_vol_row[0]:,.0f}", "inline": True},
                    {"name": "Now", "value": f"${cur_vol:,.0f}", "inline": True},
                    {"name": "Mult", "value": f"{d:.2f}×", "inline": True},
                ],
            )
            record_fired(con, 'alpha_vol_spike', 'medium', {"from": prev_vol_row[0], "to": cur_vol})

    # Holders growth — 1h-prior snapshot
    prev_h_row = con.execute(
        "SELECT holders FROM private_alpha_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
        (now - 3600,),
    ).fetchone()
    if prev_h_row and prev_h_row[0] and prev_h_row[0] > 1000:
        cur_h = int(current_snapshot.get('holders') or 0)
        d = cur_h / prev_h_row[0]
        if d >= ALPHA_HOLDERS_SPIKE_MULT and not already_fired(con, 'alpha_holders_spike', 4 * 3600):
            discord_send(
                title="🟢 Alpha holder growth spike",
                description=f"Holder count grew **+{(cur_h - prev_h_row[0]):,}** in 1h (+{(d-1)*100:.1f}%). Likely Binance Alpha campaign onboarding new users — pre-pump signal.",
                color=0x44FF44,
                fields=[
                    {"name": "Was 1h ago", "value": f"{prev_h_row[0]:,}", "inline": True},
                    {"name": "Now", "value": f"{cur_h:,}", "inline": True},
                    {"name": "New holders", "value": f"+{cur_h - prev_h_row[0]:,}", "inline": True},
                ],
            )
            record_fired(con, 'alpha_holders_spike', 'medium', {"from": prev_h_row[0], "to": cur_h})


# ─────────────────────────────────────────────────────────────────────────────
# Module 7 (was 6) — Solana cohort delta (hook into existing slx_balance_snapshots)
# ─────────────────────────────────────────────────────────────────────────────

def poll_cohort_delta():
    """Decompose cohort flow into late-claim execution, real buying, real selling.

    Patched 2026-06-01: the cohort 81% already SOLD; raw net flow was misleading
    because late TGE claim execution (was=0, now≈claim) registered as 'buying'
    when it's actually NEW SELLABLE SUPPLY entering circulation.
    """
    try:
        con = db_conn()
        latest = con.execute("SELECT MAX(ts) FROM slx_balance_snapshots").fetchone()[0]
        if not latest:
            log('cohort: no snapshots yet')
            return
        ts_1h = latest - 3600
        ts_7d = latest - 7 * 86400

        # Per-wallet 1h delta with claim context
        rows = con.execute("""
          WITH latest AS (SELECT wallet, balance_slx, liquid_claimed FROM slx_balance_snapshots WHERE ts=?),
               prev   AS (SELECT wallet, balance_slx FROM slx_balance_snapshots WHERE ts BETWEEN ?-1800 AND ?+1800 GROUP BY wallet)
          SELECT l.wallet, COALESCE(l.balance_slx,0) AS now_bal, COALESCE(p.balance_slx,0) AS prev_bal,
                 l.liquid_claimed
          FROM latest l LEFT JOIN prev p USING (wallet)
        """, (latest, ts_1h, ts_1h)).fetchall()

        late_claim_slx = 0   # was 0, now ≈ claim amount (within 5%) → late TGE claim
        real_buy_slx   = 0   # already had SLX, added more
        real_sell_slx  = 0   # had SLX, sent some out
        n_late_claim = n_real_buy = n_real_sell = 0
        for r in rows:
            delta = r['now_bal'] - r['prev_bal']
            if delta > 100:
                # Late TGE claim signature: prev_bal ≈ 0 AND delta ≈ claim_amount
                claim = r['liquid_claimed'] or 0
                if r['prev_bal'] < 10 and claim > 0 and abs(delta - claim) / claim < 0.05:
                    late_claim_slx += delta
                    n_late_claim += 1
                else:
                    real_buy_slx += delta
                    n_real_buy += 1
            elif delta < -100:
                real_sell_slx += -delta
                n_real_sell += 1

        real_net = real_buy_slx - real_sell_slx

        # Baseline: 7d-rolling avg hourly abs delta — for setting trigger threshold
        row = con.execute("""
          SELECT AVG(abs_delta) FROM (
            SELECT ts, SUM(balance_slx) - LAG(SUM(balance_slx)) OVER (ORDER BY ts) AS delta,
                       ABS(SUM(balance_slx) - LAG(SUM(balance_slx)) OVER (ORDER BY ts)) AS abs_delta
            FROM slx_balance_snapshots WHERE ts >= ? GROUP BY ts
          )
        """, (ts_7d,)).fetchone()
        baseline = row[0] if row and row[0] else 0

        # Two distinct alert paths
        # 1. Late-claim wave — new supply entering circulation
        if late_claim_slx > 30_000 and not already_fired(con, 'cohort_late_claim_wave', 3 * 3600):
            discord_send(
                title="🟡 Late-claim wave — NEW supply entering",
                description=f"{n_late_claim} wallets just executed TGE claims (was 0 → ~claim_amt).\nThese typically dump within hours per the 81% historical SOLD pattern.",
                color=0xFF8844,
                fields=[
                    {"name": "Late claims 1h", "value": f"{late_claim_slx:,.0f} SLX (~${late_claim_slx*get_slx_price_cached():,.0f})", "inline": True},
                    {"name": "Wallets", "value": str(n_late_claim), "inline": True},
                ],
            )
            record_fired(con, 'cohort_late_claim_wave', 'medium', {"late": late_claim_slx, "n": n_late_claim})

        # 2. Real accumulation OR real selling (excluding late claims)
        if baseline and abs(real_net) > COHORT_FLOW_THRESHOLD * baseline and not already_fired(con, 'cohort_real_flow', 3 * 3600):
            direction = "GENUINE SELLING" if real_net < 0 else "GENUINE BUYING"
            color = 0xFF4444 if real_net < 0 else 0x44FF44
            discord_send(
                title=f"🟡 Cohort real flow — {direction}",
                description=f"Excluding late-claim execution.\nReal 1h net: **{real_net:+,.0f} SLX**\nBuying: {real_buy_slx:,.0f} ({n_real_buy} wallets)\nSelling: {real_sell_slx:,.0f} ({n_real_sell} wallets)\nBaseline avg: {baseline:,.0f} SLX/h",
                color=color,
            )
            record_fired(con, 'cohort_real_flow', 'medium', {"net": real_net, "buy": real_buy_slx, "sell": real_sell_slx})

        log(f'cohort: late_claim={late_claim_slx:,.0f}({n_late_claim}w) real_buy={real_buy_slx:,.0f}({n_real_buy}w) real_sell={real_sell_slx:,.0f}({n_real_sell}w) baseline_h={baseline:,.0f}')
    except Exception as e:
        log(f'poll_cohort_delta error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Daemon loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log('SLX Sentinel started.')
    log(f'Webhook target: ...{WEBHOOK[-30:]}')

    last_run = defaultdict(int)
    modules = [
        ('aggregator', CAD_AGGREGATOR, poll_bsc_aggregator),
        ('alpha',      CAD_ALPHA,      poll_alpha_api),
        ('bridge',     CAD_BRIDGE,     poll_bridge_flow),
        ('perps',      CAD_PERPS,      poll_perps),
        ('square',     CAD_SQUARE,     poll_square),
        ('cohort',     CAD_COHORT,     poll_cohort_delta),
        ('price_move', CAD_PRICE_MOVE, poll_price_move),
    ]

    # Initial baseline run for all modules
    log('Bootstrapping initial baselines...')
    for name, _, fn in modules:
        try:
            fn()
            last_run[name] = int(time.time())
        except Exception as e:
            log(f'  {name} initial bootstrap error: {e}')
        finally:
            _close_active_connections()

    log('Initial baselines complete. Entering polling loop.')
    while True:
        now = int(time.time())
        for name, cadence, fn in modules:
            if now - last_run[name] >= cadence:
                try:
                    fn()
                except Exception as e:
                    log(f'{name} error: {e}')
                finally:
                    _close_active_connections()
                last_run[name] = now
        time.sleep(30)


if __name__ == '__main__':
    main()
