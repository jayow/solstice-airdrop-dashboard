export type MarketKey = 'USX-09FEB26' | 'USX-01JUN26' | 'eUSX-11MAR26' | 'eUSX-01JUN26';

export type CohortKey = '1' | '2' | '3' | '4' | '5' | '6';

export type PartnerKey = 'kamino' | 'orca' | 'raydium' | 'holdings';

export type PartnerFootprint = {
  pre: boolean;
  txs?: number;
  firstTs?: number;
  lastTs?: number;
  // Kamino only — we have parsed event details
  supplyUsd?: number;
  borrowUsd?: number;
  // Holdings only — in-wallet direct ownership of USX/eUSX
  usxCurr?: number;
  eusxCurr?: number;
  usxHeld?: boolean;
  eusxHeld?: boolean;
};

export type WalletRow = {
  addr: string;
  fee: number;
  feeTxs: number;
  first: number;
  last: number;
  cohort: CohortKey | null;
  cohortShareOfSlxPct: number | null;
  cohortUsers: number | null;
  perUserSharePct: number | null;
  claimed: boolean;
  claimTx: string | null;
  // Partner DeFi footprint. null = no activity on that partner ever.
  partners: Record<PartnerKey, PartnerFootprint | null>;
  // SLX presale journey (Legion sale). null = did not participate.
  presale: {
    deposited: number;      // gross USDC sent to vault
    refunded: number;       // USDC returned from vault
    net: number;            // deposited - refunded (what they kept)
    status: 'kept' | 'partial' | 'refunded';
    txCount: number;
    firstTs: number;
    lastTs: number;
  } | null;
  // per-market buy/sell USD (positive absolute magnitudes)
  m: Record<MarketKey, { buy: number; sell: number }>;
  // lp per underlying
  lp: {
    USX: { add: number; remove: number };
    eUSX: { add: number; remove: number };
  };
  // yield/emission claims per underlying
  claim: {
    USX: number;
    eUSX: number;
  };
  // convenience totals
  totalYtBuys: number;
  totalYtSells: number;
  totalLpAdds: number;
  totalLpRemoves: number;
  totalClaims: number;
  ytNet: number;       // buys - sells
  totalSpent: number;  // buys + addLiq
  expTxs: number;
};

export type CohortBreakdown = Record<CohortKey, {
  feePayers: number;
  claimed: number;
  orphan: number;
  shareOfSlxPct: number;
  users: number;
}>;

export type Dataset = {
  generatedAt: string;
  totals: {
    wallets: number;
    feesUsd: number;
    ytActive: number;
    ytBuys: number;
    ytSells: number;
    lpAdds: number;
    lpRemoves: number;
    claims: number;
    registered: number;
    claimedSlx: number;
    orphanFee: number;
    noCohort: number;
    cohorts: CohortBreakdown;
    partners: {
      kamino: number;
      orca: number;
      raydium: number;
      holdings: number;
      any: number;
    };
    presale: {
      // Our fee-payer subset
      buyers: number;            // fee-payers who participated in presale
      buyersKept: number;        // fee-payers who kept their deposit (no refund)
      buyersRefunded: number;    // fee-payers who got a full refund
      totalDeposited: number;    // gross USDC our fee-payers deposited
      totalRefunded: number;     // USDC returned to our fee-payers
      totalUsdc: number;         // net USDC our fee-payers kept
      // Global
      globalBuyers: number;      // net-positive buyers across the entire presale
      globalDepositors: number;  // total depositors (including fully-refunded)
      globalUsdc: number;        // on-chain net raised ($361,999)
      grossDeposits: number;     // total gross inflow to vault ($1,199,323)
      grossRefunds: number;      // total outflow from vault ($837,324)
    };
  };
  wallets: WalletRow[];
};

export type TradeEvent = {
  sig: string;
  blockTime: number;
  market: MarketKey;
  signer: string;
  action: 'buyYt' | 'sellYt' | 'addLiq' | 'removeLiq' | 'claimYield' | 'other';
  instr?: string;
  ytDelta: number;
  underlyingDelta: number;
  syDelta: number;
  usdNet: number;
  eusxRate?: number;
};
