# 2026-07-15 — VERDICT: Supply/Demand Zone Strategy FALSIFIED on DEV

Project: Zone-Strategy Falsification Pipeline (XAUUSD) — Master Spec v1.0.
Phase/Gate: Phase 2, DEV stage. Written per the master spec's pre-registered
FAIL rule ("FAIL → write lessons/YYYY-MM-DD-zone-strategy-verdict.md and
STOP"), applied at DEV — before any EVAL partition was touched.

## Symptom (what we measured)

The frozen spec (`specs/phase1_zone_spec_v1.md`, signed off 2026-07-15;
provenance in its Appendix A) was implemented on the Phase-0-verified
harness-v2 (commit `ac719c2`; 12/12 engine + 4/4 runner unit tests) and run
on DEV-2024 only. Its own pre-registered G2 bar: per-signal t ≥ +2.5,
E[R] ≥ +0.15R net, MaxDD ≤ 6% at 0.5% risk. Measured (per-signal,
canonical, costs ON, market fill; trials.csv rows 1–4):

| Trial | Config | n | PF | E[R]/sig | t (PnL) | t (R) | MaxDD | Return |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | **frozen v1.0** (impulse 1.5×ATR, age 72) | 406 | 0.745 | **−0.1432** | **−2.793** | −2.919 | 29.82% | −25.61% |
| 2 | impulse 2.0×ATR | 240 | 0.772 | −0.1255 | −1.898 | −1.938 | 17.37% | −14.23% |
| 3 | max age 24 bars | 334 | 0.775 | −0.1245 | −2.211 | −2.279 | 22.65% | −19.09% |
| 4 | impulse 2.0 + age 24 | 187 | 0.744 | −0.1411 | −1.920 | −1.922 | 13.43% | −12.59% |

Trials 2–4 were pre-declared robustness-of-negative checks, not
pass-seeking. Full detail: `reports/phase2/DEV_report.md`.

## Root cause (why it loses — three structural diagnostics)

1. **Direction-symmetric bleed.** SELL 208 signals → −$59.44/signal; BUY
   198 signals → −$66.92/signal. 2024 was a strong gold uptrend, yet longs
   at demand lost as much as shorts at supply — the loss is not trend
   exposure, it is the entry premise itself.
2. **Cost-independent.** Total costs ≈ −0.018R/signal of the −0.143R.
   Removing commission/swap entirely leaves ≈ −0.125R. Not a fee problem.
3. **Quality-tightening moves n, not expectancy.** Doubling impulse
   strength and/or quartering zone age cut signals 406 → 187 while E[R]
   stayed pinned in [−0.14, −0.12]. If zones carried per-touch edge,
   stricter zones would concentrate it; instead every quality slice loses
   at the same rate — the per-touch payoff is uniformly negative.

This is the same root cause as the falsified SMC order-block strategy
(harness-v2 calibration: legacy per-signal t +2.45 → +0.92, and live
markout showed "OB has no pull"): **retail-visible price structure
(OBs, S/D zones) carries no per-touch reversion edge on XAUUSD** —
price reaching the level is not evidence dedicated flow waits there.
The touch-entry pays adverse selection (fill + slippage against a
structural SL) with no compensating reversal probability; the 1R/2R+BE
ladder then converts a ~43% per-signal win rate into ≈ −0.13R/signal.

## Verdict / final state

**FALSIFIED at DEV under the pre-registered gate — pipeline STOPPED.**
Consistent with the master spec's own prior (P(durable edge) ~3–5%, "same
falsified FAMILY as SMC order blocks"). Total cost of the answer: $0 live,
4 trial-ledger rows, one day of DEV compute.

- **EVAL (2025-H1, 2025-H2, 2026-01..06) and FRESH OOS were deliberately
  preserved unspent** — no EVAL run was ever made, so those partitions
  remain clean for whatever hypothesis comes next (subject to the standing
  rule that EVAL overlaps spent W1–W3 and is a pre-filter only; FRESH OOS
  is the real gate).
- trials.csv is append-only history (N=4); rows stay forever.
- Harness-v2, the zone engine, the ledger, and the Track A grader remain
  reusable infrastructure; the strategy hypothesis is what died.

## Generalizable rule (one sentence)

A structure-based entry has zero prior credibility unless the *per-touch*
payoff is positive in the cheapest honest test — and if tightening the
structure's quality filters changes trade count but not per-trade
expectancy, the structure carries no edge and no amount of parameter
search will put one there.
