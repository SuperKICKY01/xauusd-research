# Lesson 2026-07-05 — Harness-v2 calibration anchor: the legacy SMC "edge" was mostly HTF lookahead + per-leg t inflation

**Context:** Phase 0 of the Zone-Strategy Falsification Pipeline (Master Spec v1.0).
Deliverable 0.3: re-run the frozen legacy SMC config on the SAME data as
`report_costs_065.json` under the repaired harness, report old-vs-new side by side.
**Status:** Calibration evidence. No strategy change proposed. This file is the
harness-v2 anchor all future comparisons cite.

## What was fixed (harness-v2, Phase 0.1 + 0.2)

1. **HTF lookahead (0.1):** `backtest/data_loader.py::slice_window` sliced
   `df.loc[:ts]` with `ts` = the entry bar's OPEN time on open-time-labelled
   frames, so the still-FORMING H1/M15 bar (up to ~55 min of future data) was
   included in every window whose decision step didn't land on the HTF
   boundary — 91.5% of M5 steps for H1 (unit test measured; theory 55/60 = 91.7%).
   Fix: windows may contain only bars whose CLOSE ≤ decision time (= entry bar
   close). Mandatory unit test: 1,000 random replay steps on the real CSV,
   100% pass (`tests/test_no_lookahead.py`).
2. **Per-leg t-stats (0.2):** TP1/TP2 legs of one signal are correlated;
   per-leg t inflates up to ~√2. Canonical stats are now per-SIGNAL
   (`signal_id` minted in `VirtualBroker._spawn_legs`; grouped in
   `backtest/metrics.py`; WFV gate switched to per-signal t). Measured
   inflation on the V1 leg: per-leg t 3.345 vs per-signal t 2.453 (×1.36).

## Calibration design

The recorded artifact (2026-05-31) could not be compared to a single re-run
because strategy code drifted since (dual-TP semantic fix 06-11, SMC
degenerate-signal fix 06-13, …). So: TWO legs on IDENTICAL current strategy
code, same data (`data/xauusd_m5_full.csv`, 165,437 bars, 2024-01-01 →
2026-05-06), same frozen config (MIN_SCORE=75, risk 0.65%, costs ON,
**limit fill** — note: the artifact was limit-fill per
[[2026-05-30-transaction-costs]], not market-fill as the master spec
misremembers), HMM/DD-mod/time-stop OFF (all post-date the artifact),
differing ONLY in the slice_window rule:

- **V1** = legacy slicing (lookahead present) — isolates code drift.
- **V2** = harness-v2 slicing — the honest number.

Reconstruction of the V1 tree was verified hunk-identical to
`reports/phase0/diff_0.1_lookahead_fix.patch`, and V1's replay determinism was
verified against its first (crashed) attempt at bar 54,200
(signals=101, balance=$109,242.95 — byte-identical).

## Results — old vs new, side by side (the spec-required table)

| | Recorded 2026-05-31 (`report_costs_065.json`) | V1: current code, LEGACY slicing | V2: current code, HARNESS-V2 |
|---|---:|---:|---:|
| Signals emitted → placed → filled | 482 → 188 → 98 | 727 → 314 → 89 (28.3%) | 629 → 303 → 77 (25.4%) |
| Closed legs | 196 | 178 | 154 |
| Per-LEG PF / t (legacy stats) | 1.402 / **+3.161** | 1.490 / +3.345 | 1.220 / **+1.250** |
| Per-SIGNAL n | 98 | 89 | 77 |
| Per-SIGNAL PF | — (not computed then) | 1.748 | 1.258 |
| Per-SIGNAL E[R] net | +0.308 (FTMO-MC recon) | +0.346 | **+0.133** |
| Per-SIGNAL t (PnL / R) | — | +2.453 / +2.531 | **+0.917 / +0.928** |
| Win rate (signal) | 53.1% | 53.9% | 46.8% |
| Return (28.2 mo) | +21.19% | +21.69% | **+6.67%** |
| Max DD | 5.438% | 4.817% | 5.430% |

Run artifacts: `report_calib_v1_full.json`, `report_calib_v2_full.json`,
logs `bt_calib_v1.err.log`, `bt_calib_v2.err.log` (crash of V1 attempt 1:
`reports/phase0/bt_calib_v1_attempt1_crash.log` — flaky pandas
`SystemError` in block consolidation; attempt 2 reproduced attempt 1's state
exactly and completed).

## Decomposition of the legacy t = 3.16

1. **Code drift** (recorded → V1): t 3.161 → 3.345 per-leg. Small aggregate
   effect; selection changed materially (98 → 89 signals from 482 → 727
   emissions) but PnL/t similar. Drift is NOT the story.
2. **Lookahead removal** (V1 → V2, the harness effect): per-signal t
   2.453 → 0.917; E[R] +0.346 → +0.133 (−62%); return +21.7% → +6.7%
   (−69% of PnL); DD unchanged (~5%). The forming H1 bar was feeding both
   signal SELECTION (89 → 77 fills) and outcome quality.
3. **Per-leg inflation** (0.2): a further ×1.36 of apparent significance on
   top, in the legacy reporting.

**Bottom line: under the honest harness the frozen legacy SMC config shows
per-signal t ≈ +0.92 over 28.2 months — statistically indistinguishable from
zero.** This retroactively explains the OOS falsification (t=+0.09, PF 0.98):
the in-sample "edge" was predominantly harness artifact, and the artifact does
not survive out of sample because it never was an edge.

DD did NOT shrink with the edge (5.43% at 0.65% risk) — variance stayed,
expectation left.

## Generalizable rule

**A backtest window slicer on open-time-labelled frames must cut by bar CLOSE
time vs decision time, never by label ≤ now; and significance must be computed
on the independent unit (signal), never the execution unit (leg).** Both bugs
inflate in-sample edge in the same direction, they compound, and neither shows
up out of sample — if IS strength collapses OOS, audit the harness before the
strategy.

## Proposed SKILL.md anti-pattern addition (needs approval — not auto-applied)

> 9. **Never slice HTF windows by label ≤ decision ts.** Open-time-labelled
>    frames must be cut by bar CLOSE ≤ decision time
>    (`slice_window(..., bar_minutes)`), or the forming bar leaks ~55 min of
>    future into every H1 window. And report significance per SIGNAL
>    (`metrics per_signal`), never per leg — legs are correlated (~√2
>    inflation). See lessons/2026-07-05-harness-v2-calibration-anchor.md.

## What this lesson does NOT claim

- Does NOT claim the legacy strategy has zero edge with certainty — it claims
  the evidence for edge, under an honest harness, is n=77 / t=0.92 / E[R]=+0.13,
  which is far below any deployment gate we use (t≥2.5).
- Does NOT validate harness-v2 against live fills — that is Phase 3 (G3).
  Known residual gap: LIVE windows include the partial forming bar
  (`copy_rates_from_pos(..., 0, N)`), harness-v2 excludes it entirely; the
  backtest is deliberately the conservative side of that gap (no partial-bar
  reconstruction in v1, per spec).
- Does NOT re-tune anything. Frozen config was frozen.

## Related lessons

- [[2026-05-30-transaction-costs]] — the recorded artifact this calibrates against.
- [[2026-05-29-walk-forward-results]] — the OOS collapse this lesson explains.
- [[2026-06-10-backtest-on2-and-desync]] — earlier audit that touched slice_window and called it a FALSE lead for the desync; the lookahead was real regardless.
