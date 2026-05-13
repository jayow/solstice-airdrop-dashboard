"""Shared YT-holder enumeration for Exponent YT walkers.

Replaces the audit-file dependency with direct on-chain enumeration. Each
walker invocation:
  1. Fetches the market's yt_mint + yp_alias from on-chain market account
  2. Enumerates every Exponent program account with disc e35c92, size 164
  3. Filters by yp_alias == market's alias → set of active YT holder wallets
  4. Unions with any wallet that has a cached S2_EXPONENT_YT extraction
     (catches closed positions whose owner is no longer in size-164 set)
"""
import sys, os, base64, base58, struct
THIS = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(THIS) not in sys.path: sys.path.insert(0, os.path.dirname(THIS))
from rpc_helper import rpc
import db

EXPONENT_CORE = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
V2_DISC_HEX = 'e35c92311d55475e'


def market_meta(market_pk: str) -> dict:
    """Return {'yt_mint': ..., 'yp_alias': ..., 'yt_supply': ...} for a market."""
    r = rpc('getAccountInfo', [market_pk, {'encoding': 'base64'}], timeout=15)
    d = base64.b64decode(r['result']['value']['data'][0])
    yt_mint  = base58.b58encode(d[40:72]).decode()
    yp_alias = base58.b58encode(d[104:136]).decode()
    rs = rpc('getAccountInfo', [yt_mint, {'encoding': 'jsonParsed'}], timeout=15)
    info = (rs.get('result', {}) or {}).get('value', {}).get('data', {}).get('parsed', {}).get('info', {}) or {}
    supply = float(info.get('supply') or 0) / (10 ** int(info.get('decimals') or 6))
    return {'yt_mint': yt_mint, 'yp_alias': yp_alias, 'yt_supply': supply}


def active_yt_holders(yp_alias: str) -> dict:
    """Returns {wallet: yt_amount_total} for every active YT position on this market.
    Uses dataSize=164 + disc=e35c92 filter, then yp_alias match at offset 40."""
    r = rpc('getProgramAccounts', [EXPONENT_CORE, {
        'encoding': 'base64',
        'filters': [
            {'dataSize': 164},
            {'memcmp': {'offset': 0, 'bytes': base58.b58encode(bytes.fromhex(V2_DISC_HEX)).decode()}},
            {'memcmp': {'offset': 40, 'bytes': yp_alias}},
        ],
    }], timeout=180)
    out = {}
    for a in r.get('result') or []:
        d = base64.b64decode(a['account']['data'][0])
        if len(d) < 80: continue
        authority = base58.b58encode(d[8:40]).decode()
        yt_amount = struct.unpack('<Q', d[72:80])[0] / 1e6
        if yt_amount > 0:
            out[authority] = out.get(authority, 0.0) + yt_amount
    return out


def yt_holder_universe(yp_alias: str) -> list:
    """Union of currently-active holders (on-chain) and historically-extracted
    wallets in DB.quest_cache (captures closed positions whose owner is no
    longer in the size-164 set)."""
    wallets = set(active_yt_holders(yp_alias).keys())
    for r in db.conn().execute("SELECT DISTINCT wallet FROM quest_cache WHERE quest_key='S2_EXPONENT_YT'"):
        wallets.add(r['wallet'])
    return sorted(wallets)
