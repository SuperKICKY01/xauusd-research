# Portfolio-level Time-Series Momentum (Test 19) — results & verdict

**Date:** 2026-06-20 · **Cost:** $0 (yfinance ETF/FX adj-close + cached Binance crypto) · **Bot 6168 untouched.**
Reproduce: `fetch_yf.py` (caches), then `tsmom_portfolio_phase1.py` (`run_log.txt` = captured run). Deterministic (no RNG).

## Verdict: **PASS the pre-registered gate** — the first validated strategy in the program.
…but it is a known, capacity-constrained institutional strategy, **not executable at $10k and not portable
to the XAUUSD bot.** Read the caveats — the "pass" is real but heavily qualified.

| Gate condition | Result |
|---|---|
| CPCV mean OOS Sharpe > 0 | **✓** 0.86 (median 0.85, std 0.21, **100% of folds >0**, picks L=252) |
| Portfolio DSR clears p<0.05 | **✓** 0.982 @ honest N=86 — **passes even at N=200 (0.965)** |
| Spanning α significant, low equity-β | **✓** α=+11.4%/yr, **t=4.5, p<0.001, β=0.05** vs SPY |
| Premium not confined to one early subsample | **✓** sig pre-2015 (t=3.6) AND 2020-26 (t=2.1); only choppy 2015-19 weak |
| Robust to lookback / seed | **✓** all L>0 (252→0.89, 126→0.65, 63→0.60); tanh & sign both work; deterministic |

## Setup
- **Universe (41):** 37 ETF/FX (yfinance, **dividend-adjusted total return**) + 4 crypto (Binance):
  commodities (GLD SLV PPLT PALL USO UNG DBA DBB CPER), equities (SPY QQQ IWM EFA EEM EWJ EWZ FXI VGK
  INDA EWA EWY EWG), rates/credit (TLT IEF SHY LQD HYG TIP EMB AGG), FX (UUP FXE FXY FXB FXA FXF),
  REIT (VNQ), crypto (BTC ETH BNB SOL). **Point-in-time membership** (each enters at its own inception;
  no backfill). ETFs all still listed (no delisting survivorship); crypto majors all alive.
- **Construction:** per-asset TSMOM = tanh(vol-normalised trailing-L return), L∈{63,126,252}d → per-asset
  inverse-vol sizing (risk parity, capped 3×) → **monthly** rebalance → **portfolio vol-target 12%**
  (causal) → fractional Kelly λ=0.3 (economics only). Costs: ETF 2bp/side, crypto 8bp/side on turnover.
- **Causality:** every estimate trailing+shifted; weights acted monthly, ffilled, shifted ≥1d. Stats run on
  the portfolio-vol-targeted net series (the actual strategy). Annualisation 252 (business-day calendar).

## Key tables (headline L=252 tanh, net, 12% target)
- **Full sample:** annSR **0.89**, annRet 11.9%, annVol 13.4%, maxDD −27%, ~6 month-turnover.
- **Subsample / decay:** pre-2015 0.90 · 2015-19 0.74 · 2020-26 0.96 — **no structural decay** (the weak
  2015-19 matches the documented mid-2010s trend drought; 2022 bond/rate trend + crypto lift 2020-26).
- **Cost sensitivity:** 0bp 0.90 → 2bp 0.89 → 5bp 0.87. **Costs are not a killer** (monthly = low turnover).
- **Look-ahead / placebo (decisive):** +1d lag 0.86, +4d lag 0.83 (no imminent-info leak); **reversed
  (anti-mom) = −0.92** (symmetric → edge IS the directional bet); **raw, no port vol-target = 0.64**
  (pure trend-alpha, in literature range; vol-targeting adds the rest).
- **DSR:** T=6803, **skew −0.20, exKurt +3.4** (diversification normalised the returns — vs Test 18's
  −3.2/+74). DSR 0.9995→0.965 across N=6→200, all PASS.

## Why it passes where Tests 17/18 failed (and the honesty on it)
Single-asset gold (T17) had no breadth → fat-tailed, DSR-impossible. Cross-sectional crypto (T18) was
beta-neutral but decayed and crash-tailed. Here, aggregating ~independent trend bets (a) raises Sharpe
and (b) **normalises the return distribution** (CLT), which is the real mechanism that lets portfolio-DSR
pass. The methodology critique was correct.

## CAVEATS — what still qualifies the pass (do NOT over-deploy)
1. **Effective breadth is only ~4, not 41.** Avg pairwise corr +0.23 ⇒ N_eff ≈ 4 independent bets. The
   "41 instruments" oversells diversification; the power is real but modest and concentrated in a few
   blocks (rates / equity / commodity / crypto).
2. **Sharpe is at the optimistic end.** 0.89 net > the conservative 0.3-0.7 post-2010 target. Decomposes
   into raw trend 0.64 (in-range) + ~0.25 vol-timing uplift; sample is ~56% pre-2015 (strong TSMOM era)
   and includes crypto's 2020+ mega-trends. **Live, post-2010, ex-crypto would likely be ~0.4-0.6.**
3. **Spanning controls only for equity.** TSMOM overlaps carry/bond/commodity premia; "alpha" here is
   alpha-vs-equity, not vs all factors. Multi-factor spanning is a Phase-2 must.
4. **ETF proxies, not futures.** Expense ratios, short-borrow (modelled **free** = optimistic), and roll
   decay (USO/UNG) contaminate the proxy. Returns are vs 0, not vs risk-free; financing on levered/short
   legs unmodelled.
5. **Not executable at $10k.** 41 levered positions incl. shorts and >1× bond legs need ~6-figure capital
   + futures/margin/borrow access. At $10k you are forced into a degenerate subset → lose the breadth that
   IS the edge. Economics: full 12% vol ≈ +12%/yr (notional, not executable); Kelly-0.3 (4% vol) ≈
   **+3.7%/yr ≈ $367/yr on $10k**. Statistically real, capital-hungry, modest in $ at retail scale.
6. **Orthogonal to the project goal.** This is diversified managed-futures trend — NOT portable to the
   single-symbol, short-horizon, tight-DD FTMO XAUUSD challenge that bot 6168 targets.

## Recommendation
Genuine, clean PASS — methodology vindicated. But it's the right edge in the wrong vehicle for this
account: capacity-fine for institutions, impractical at $10k, and not a XAUUSD prop-firm solution.
**STOP for review.** If pursued, Phase 2 must: multi-factor spanning, real futures data + roll/borrow/
financing costs, ex-crypto robustness, and an honest capital-adequacy / executable-subset study.
