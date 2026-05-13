// Known Exponent markets for Solstice (USX, eUSX).
// Live markets via https://api.exponent.finance/markets.
// Expired markets discovered via on-chain inspection of older Exponent txs.
//
// Note: the user reported "March 11" expiries — the actual earlier USX maturity
// on-chain is 2026-02-09. No matching earlier eUSX market was found; eUSX
// appears to have launched with the 2026-06-01 market only.
export const MARKETS = {
  'USX-09FEB26': {
    ticker: 'USX',
    maturity: '2026-02-09',
    expired: true,
    underlying: '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', // USX
    underlyingDecimals: 6,
    underlyingUsdPrice: 1.00,
    ytMint: 'HQmMS5W34VcMtR85akhZgvypy7iqVWRXi282vwdf9eTX',
    ptMint: '7vWj1UriSscGmz5wadAC8EkA8ndoU3M7WUifqxTC3Ysf',
    syMint: '4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT', // wUSX (shared)
    scrapeAddresses: [
      'HQmMS5W34VcMtR85akhZgvypy7iqVWRXi282vwdf9eTX', // YT mint
      '9t936gEYkXJ5tFEMA6DnRVNwUTwNRRSQ87zocwou16gz', // market PDA (YT mint authority) — catches claims
    ],
  },
  'USX-01JUN26': {
    ticker: 'USX',
    maturity: '2026-06-01',
    expired: false,
    underlying: '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',
    underlyingDecimals: 6,
    underlyingUsdPrice: 1.00,
    ytMint: 'Au8g11nXqXrUAmL14GM3gQnrnJnr4dcpgc5DNAnu9F9s',
    ptMint: '3kctCXgt6pP3uZcek8SqNK2KZdQ6cqtj9hc3U46jhgBk',
    syMint: '4CEd2syXcV8rAiwFkdCkpmTBsgGVS7NcFnygf86EG2KT',
    vault: '4hZugBhgd3xxShK5iHbBAwCnJUjthiStT6LnruRwarjr',
    scrapeAddresses: [
      'Au8g11nXqXrUAmL14GM3gQnrnJnr4dcpgc5DNAnu9F9s', // YT mint
      '3oAfRGTEmeDeN8HZYCv2tMeLGMoRpPs4Jyo11wm8JcdV', // orderbook
      'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm', // legacy market
      '4hZugBhgd3xxShK5iHbBAwCnJUjthiStT6LnruRwarjr', // SY reserve vault
      'DjDHnfWtVsAgNZJtLj8UWxBBYbBPC9xV69KHt7SzEXXy', // market PDA (YT mint authority)
    ],
  },
  'eUSX-11MAR26': {
    ticker: 'eUSX',
    maturity: '2026-03-11',
    expired: true,
    underlying: '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
    underlyingDecimals: 6,
    underlyingUsdPrice: 1.00,
    ytMint: 'DDoYyEUcdkHV5a4NCPXDRL9f93NgPbqK9ZANAGL627wF',
    ptMint: '6oiDcfve7ybKUC8ysZmncC9iSuxQG2vrRkh3dgV7EKR4',
    syMint: '7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR', // shared SY across eUSX markets
    scrapeAddresses: [
      'DDoYyEUcdkHV5a4NCPXDRL9f93NgPbqK9ZANAGL627wF', // YT mint
      '8QshMo7i8RRKxPuU4kgbKVowCieKV1nf9H7Ycii2ZSXt', // market PDA (YT mint authority) — catches claims
    ],
  },
  'eUSX-01JUN26': {
    ticker: 'eUSX',
    maturity: '2026-06-01',
    expired: false,
    underlying: '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
    underlyingDecimals: 6,
    underlyingUsdPrice: 1.00,
    ytMint: 'GEYwnvNzqFXrLnNq4riXbn2ASnwU3cF8RXW6wXKHM4sw',
    ptMint: 'BNR2FsHo8JrYGWx2V8yxG5GBWiG3uU8voi2eMGBHFwEj',
    syMint: '7EtXTvy1NBEo51N3Bj3VYafgDFfPcTy5sjpVZvVGiiyR',
    vault: '7NviQEEiA5RSY4aL1wpqGE8CYAx2Lx7THHinsW1CWDXu',
    scrapeAddresses: [
      'GEYwnvNzqFXrLnNq4riXbn2ASnwU3cF8RXW6wXKHM4sw', // YT mint
      '7NviQEEiA5RSY4aL1wpqGE8CYAx2Lx7THHinsW1CWDXu', // SY reserve vault
      'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP',  // legacy
      'BnqAo2Lpmg7BNP3mCUKBXRq5SFPqBLo6oDnqmsfUSpDG', // market PDA (YT mint authority)
    ],
  },
};

export const EXPONENT_PROGRAM = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7';

// Map mint -> market key for fast lookup
export const YT_TO_MARKET = Object.fromEntries(
  Object.entries(MARKETS).map(([k, v]) => [v.ytMint, k])
);
export const UNDERLYING_TO_MARKET = Object.fromEntries(
  Object.entries(MARKETS).map(([k, v]) => [v.underlying, k])
);
export const SY_TO_MARKET = Object.fromEntries(
  Object.entries(MARKETS).map(([k, v]) => [v.syMint, k])
);
