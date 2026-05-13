"""Kamino 'Solstice Market' (a.k.a. eUSX leverage pool) — reserve configuration.
Source: https://api.kamino.finance/kamino-market/9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU/reserves/metrics
"""

KAMINO_LEND_PROGRAM = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
SOLSTICE_MARKET     = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'

# Each reserve:
#   symbol        display
#   reserve       Kamino reserve account (scrape this for sigs)
#   mint          liquidity token mint
#   decimals      mint decimals
#   px            USD price approximation (stablecoins = $1, PT close to $1 for short-dated)
#   kind          'collateral' | 'borrow' | 'both'
RESERVES = {
    # Collateral-only (maxLtv > 0, borrowApy ~ 0 because users can't borrow PT/eUSX from here)
    'PT-USX-9FEB26':  dict(reserve='DARCdqsV1SjdiLM9d5wLqex49TKrUX6NtuN6vFogrWWQ', mint='7vWj1UriSscGmz5wadAC8EkA8ndoU3M7WUifqxTC3Ysf', decimals=6, px=1.00, kind='collateral'),
    'PT-USX-01JUN26': dict(reserve='BLKW7xCY5g5qE8S5Z3riw7TYRQnm8NMfeqB9qb269Bo3', mint='3kctCXgt6pP3uZcek8SqNK2KZdQ6cqtj9hc3U46jhgBk', decimals=6, px=1.00, kind='collateral'),
    'PT-eUSX-11MAR26':dict(reserve='4Z7Jhj7iAg2WhPBNtPSUAujeQKKpBhbnaijGhTMKHoDK', mint='6oiDcfve7ybKUC8ysZmncC9iSuxQG2vrRkh3dgV7EKR4', decimals=6, px=1.00, kind='collateral'),
    'PT-eUSX-01JUN26':dict(reserve='EzmztxShSt8AwpBBbJxpYaKAY3E3PWQCyPQPkUYbP9u', mint='BNR2FsHo8JrYGWx2V8yxG5GBWiG3uU8voi2eMGBHFwEj', decimals=6, px=1.00, kind='collateral'),
    'eUSX':           dict(reserve='ARQFJTiUJEuxoiA9VtAcnoAUHYvbTmhKytz7D6nfnfEb', mint='3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', decimals=6, px=1.00, kind='collateral'),
    # Borrow-only reserves (maxLtv = 0) — users can supply here (earn) OR borrow
    'USX':  dict(reserve='H2pmnDSjfxeQ8zUeyUohokegYbXZgkjH4kgmoQVybyAX', mint='6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',                                      decimals=6, px=1.00, kind='borrow'),
    'USDC': dict(reserve='6XdN3zXeoYKgfSeZb8h1LpiEkUXRJ3CbimE5FJ35XFBP', mint='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',                                      decimals=6, px=1.00, kind='borrow'),
    'USDG': dict(reserve='34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83', mint='2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',                                      decimals=6, px=1.00, kind='borrow'),
}

MINT_TO_RESERVE = {r['mint']: (sym, r) for sym, r in RESERVES.items()}
RESERVE_TO_SYM  = {r['reserve']: sym for sym, r in RESERVES.items()}
