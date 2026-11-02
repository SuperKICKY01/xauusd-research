# Cross-Sectional Crypto Momentum — Phase 1 results & verdict

**Date:** 2026-06-20 · **Cost:** $0 (public Binance daily klines) · **Bot 6168 untouched.**
Reproduce: `fetch_binance.py` (caches), then `xs_momentum_phase1.py`. Deterministic (no RNG).

## Setup
- **Universe pool:** 66 USDT spot pairs, 2019-01-01 → 2026-06-20 (2693 daily bars used).
- **Survivorship handled:** point-in-time entry (coin enters at its own listing date) + exit
  (leaves universe when its data ends = delisting). Pool **deliberately includes 12 in-sample
  deaths** the short leg would have caught: SRM (FTX), ANC/MIR (Terra), WAVES, OMG, BTS, MATIC→POL,
  EOS, FTM→S, KEY, NULS, DENT. At each weekly rebalance, top-20 by trailing 30d $-volume
  (point-in-time liquidity, **not** today's winners). Residual gap stated below.
- **Strategy:** weekly rebalance, rank by trailing momentum, long top quantile / short bottom
  quantile, **dollar-neutral** (long Σw=+1, short Σw=−1). Grid: lookback∈{7,14,30}d, quantile∈{20,30}%,
  weighting∈{equal,inv-vol}. Taker fee charged on **all** turnover (4/8/10 bp tested).
  Stats run on the fixed-gross long-short **spread**; 15% vol-target applied only for $-economics.
  Shorting modelled **free-to-borrow (no perp funding)** = optimistic case.

## Key tables (fee = 8 bp/side unless noted)

Headline cfg = L14 / q0.30 / equal. Full-sample annSR = **0.64** (grid range 0.39–0.81).
Raw spread vol is huge (54–99%/yr) and raw maxDD catastrophic (−49% to −99%) → vol-targeting essential.

**Fee sensitivity (headline):** turnover ≈ **1.98/wk (~99% of book traded weekly)**.
annSR 0bp→10bp: 0.77 → 0.71 → 0.64 → 0.61. annRet 47%→37%. *Costs are a real ~10%/yr drag, not fatal.*

**Subsample / decay (headline):**
| window | obs | annSR | annRet% | maxDD% |
|---|---|---|---|---|
| full | 2693 | 0.64 | 39.1 | −71.2 |
| early ≤2021 | 1061 | 0.74 | 56.1 | −71.2 |
| mid 22–23 | 730 | **1.03** | 53.4 | −40.1 |
| late 24+ | 902 | **0.16** | 7.4 | −52.3 |

**Spanning `r_strat = α + β·r_bench` (HAC):** β ≈ −0.04 (vs BTC) / −0.06 (vs EW index) → **genuinely
market-neutral** (this is the structural win vs gold TSMOM, which was all beta).
α full-sample = +41–43%/yr, t≈2.0–2.1, **p = 0.036–0.044** (borderline-significant *pooled*) —
but α is **insignificant in every individual subsample** (p 0.12–0.15 early/mid) and **dead late**
(α t = 0.22–0.41, p = 0.68–0.83).

**CPCV (N=6,k=2, cfg picked on train, OOS eval, purged+embargoed):** OOS annSR mean **0.204**,
median 0.295, std 0.54, min −1.15, max 0.95; **73% of folds > 0**. Positive but weak/noisy.

**Deflated Sharpe (headline, full sample):** T=2693, annSR 0.639, **skew −3.19, exKurt +74.2**
(extreme crash-tails). honest N = 36 (this study) + ~40 (prior program) = 76.
DSR = 0.49 (N=12) → 0.33 (N=30) → **0.21 (N=76)** → 0.17 (N=120). **Fails p<0.05 at every N**,
including N=12. Driven by both selection and severe non-normality.

**Absolute economics (vol-targeted 15%):** CAGR +12.3%/yr ≈ **+$1,231/yr on $10k**, maxDD −22.8% —
but that average is dominated by 2019–2023; the live-relevant 24+ regime is ~flat risk-adjusted.

## Verdict vs PRE-REGISTERED gate → **FAIL**
- CPCV mean OOS Sharpe > 0 — **✓** (0.20, weak)
- Spanning α p<0.05 with β≈0 — **β≈0 ✓**, **α only borderline-sig pooled, fails all subsamples ✗**
- DSR clears p<0.05 — **✗** (0.21 at honest N; 0.49 even at N=12)
- Not fragile to lookback/quantile/weighting/subsample — **✗** (SR 0.39–0.81 across cfgs; severe post-2024 decay)

**Dominant killer = (c) DECAY / arbitraged-away** (SR 1.03 in 22–23 → 0.16 in 24+, exactly the
Liu-Tsyvinski post-2021 decay), compounded by **extreme negative-skew tail risk** (DSR fail).
**(b) beta is NOT the problem** — the long-short genuinely cancels market beta (the one thing that
worked, and the way it differs from the failed single-asset gold TSMOM). **(a) costs** are a real
~10%/yr drag but secondary.

**Honest caveats:** survivorship mitigated but not eliminated (pre-rename LUNA crash fragmented;
truly-unknown dead tickers absent — both would, if anything, *flatter* the recent survivor sample,
so the decay is real despite them). Shorting modelled free-to-borrow (optimistic; real perp funding
only adds drag). Intra-week weight drift unmodelled (second-order). Returns are wildly non-normal,
so the Sharpe itself is an unreliable summary here.

**Recommendation:** do NOT proceed to Phase 2 / capital. The documented edge class is real-ish
historically but beta-neutral-and-decayed; at $10k with this tail profile it is not economically
meaningful. STOP for review.
