# PHASE 2 — DEV STAGE REPORT (pre-G2): Zone Strategy v1.0 on DEV-2024

Project: Zone-Strategy Falsification Pipeline (XAUUSD) — Master Spec v1.0
Role: BUILDER (Claude). Date: 2026-07-15.
Phase/Gate addressed: **Phase 2, DEV stage only. No EVAL partition touched.**
Status: **STOPPED for user review** (per Phase-1 sign-off instruction).

## Implementation (committed `ac719c2`)

`quant/zone_engine.py` + `backtest/zone_runner.py` + `backtest/zone_cli.py`
+ `tools/trials_ledger.py`, implementing the frozen spec
(`specs/phase1_zone_spec_v1.md`) on the Phase-0-verified VirtualBroker
market path. Unit tests: **12/12 engine, 4/4 runner** (hand-computed
ATR/geometry fixtures; end-to-end fill/TP/BE/gap/concurrency on synthetic
M5). G0 suites unaffected (3/3 per-signal re-run). Harness-v2 slicing is
structural (H1 feed pointer, close ≤ decision time, asserted every feed).

Spec-ambiguity resolutions (documented, not tuned):
1. Activation bar = max(k, i+2) — causality (fractal unknowable before i+2).
2. Touch eligibility starts at the activation bar's H1 CLOSE (a graze inside
   the activation hour itself pre-dates activation; unit-tested).
3. No time-based exit: the broker's legacy 96-bar TIMEOUT was disarmed
   (spec §1.1 declares SL/TP1/TP2/BE as the exhaustive exit set). The only
   TIMEOUT rows are the end-of-replay flush (2 legs, boundary artifact).

## Results — all runs ledgered (trials.csv rows 1–4, N = 4)

DEV = 2024 only (`data/xauusd_m5_full.csv`, .loc[2024-01-01 : 2025-01-01]).
Costs ON, market fill, risk 0.5%, per-signal canonical stats.

| Trial | Config | n sig | E[R]/sig | t (PnL) | PF | MaxDD | Return |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | **frozen v1.0** | 406 | **−0.1432** | **−2.793** | 0.745 | 29.8% | −25.6% |
| 2 | impulse 2.0×ATR | 240 | −0.1255 | −1.898 | 0.772 | 17.4% | −14.2% |
| 3 | max age 24 bars | 334 | −0.1245 | −2.211 | 0.775 | 22.6% | −19.1% |
| 4 | both (2.0 + 24) | 187 | −0.1411 | −1.920 | 0.744 | 13.4% | −12.6% |

Trials 2–4 were pre-declared as a robustness-of-negative check (the two
obvious zone-quality axes), not pass-seeking; declared before results.

Reports on disk (regenerable, deterministic; untracked — 6 MB each):
`reports/phase2/report_zone_dev_t{1..4}.json`.

## Diagnostics (trial 1)

- **Direction-symmetric bleed**: SELL 208 signals, −$59.44/signal; BUY 198
  signals, −$66.92/signal. NOT a 2024-uptrend artifact — zone touches have
  no reversion edge in either direction.
- **Costs are not the story**: total costs $3,291 over 406 signals ≈
  −0.018R/signal of the −0.143R. Removing costs entirely leaves ≈ −0.125R.
- **Quality axes don't move expectancy**: tightening impulse/freshness cuts
  n by 54% but E[R] stays in [−0.14, −0.12] — the per-touch payoff is
  uniformly negative, the signature of a structurally edgeless premise
  (consistent with the RR-rejection finding on OBs: no pull at the zone).
- Funnel (trial 1): 5,912 H1 bars → 1,595 swings → 656 zones → 508 touches
  → 406 signals (74 concurrency-skips, 28 gap-skips). Leg outcomes: 466
  LOSS / 263 WIN / 81 BE / 2 flush.

## Distance to the pre-registered bar (G2, verbatim excerpts)

> Per-signal t >= 2.5 … Net expectancy >= +0.15R after costs … Max DD
> (bar-close) <= 6% at 0.5% risk.

Measured: t = −2.8 (wrong side, significant), E[R] = −0.14R (0.29R short),
MaxDD = 29.8% (5× over). The frozen spec is not near the bar — it is
significantly on the other side of zero.

## Options for the review

(a) **Verdict now — RECOMMENDED.** DEV falsifies the premise: significantly
    negative, direction-symmetric, robust to quality tightening, and
    consistent with the pre-registered prior (P(edge) 3–5%, same family as
    the falsified SMC order blocks). Write
    `lessons/2026-07-15-zone-strategy-verdict.md`, do not burn EVAL.
(b) Small pre-registered DEV grid on the remaining Category-B axes
    (height guards, confirm window). Expected value low: expectancy was
    flat across the axes already swept, and every row raises the DSR N.
(c) Run EVAL with the frozen spec. Pointless-and-costly: DEV already
    rejects at t −2.8; an EVAL pass would be uninterpretable luck and the
    partition would be spent.

Per the standing directive: EVAL would in any case be a pre-filter only —
FRESH OOS is the real gate. Nothing here earns that spend.

**Awaiting review. No EVAL run, no respec, no further DEV rows until
sign-off.**
