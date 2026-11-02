# PHASE 0 — HARNESS REPAIR: GATE G0 REPORT

Project: Zone-Strategy Falsification Pipeline (XAUUSD) — Master Spec v1.0
Role: BUILDER (Claude). Date: 2026-07-05.

## Gate text (quoted verbatim from the Master Spec)

> GATE G0: unit test passes; per-signal stats implemented; calibration
> documented. No strategy work may begin before G0.

## Verdict: **G0 PASS** — all three conditions met, evidence below.

---

## 0.1 HTF lookahead fix

Pre-registered deliverable (verbatim):

> Rule: at an M5 decision step whose bar CLOSES at time T, the primary
> (H1) and secondary (M15) windows may contain ONLY bars whose close
> time <= T. Simplest correct fix: after slicing df.loc[:end_ts],
> DROP the last HTF row if (row_open_time + tf_duration) > T.
> [...]
> UNIT TEST (mandatory): iterate 1,000 random replay steps; assert
> max(HTF bar close time) <= decision time. Must pass 100%.

Implementation: `backtest/data_loader.py::slice_window(full_df, decision_ts,
bars, bar_minutes)` keeps bars with open ≤ decision_ts − bar_minutes, which is
exactly the set with close ≤ T (single inclusive slice ⇒ the rule holds for
every row, not only the last — a strict superset of the spec's drop-last rule;
equivalence for label-left frames noted in the docstring). Call sites updated:
`backtest/strategy_runner.py` (decision_ts = entry bar close),
`tools/_expiry_fillrate_sweep.py`. Partial-bar reconstruction NOT attempted
(per spec). Diff: `reports/phase0/diff_0.1_lookahead_fix.patch`.

**Unit test result (mandatory):** `tests/test_no_lookahead.py` on
`data/xauusd_m5_full.csv` — **1000/1000 random replay steps pass (100%)**;
asserts cover all three frames + full-window length + entry-window ends
exactly at the decision bar. Control measurement: the harness-v1 rule
included a FORMING H1 bar on 915/1000 steps (91.5%; theory 55/60 = 91.7%).
Reproduce: `.\venv\Scripts\python.exe -m tests.test_no_lookahead` (fixed seed).

## 0.2 Per-signal statistics

Pre-registered deliverable (verbatim):

> Group closed legs by signal_id; compute per-SIGNAL net R and PnL;
> t-stat on per-signal values (ddof=1). Report BOTH per-leg (legacy,
> for comparison) and per-signal (canonical) — all gates use
> per-signal.

Implementation: `signal_id` minted per signal at the single leg-construction
choke point (`backtest/virtual_broker.py::_spawn_legs`, both fill models),
carried on `Position` + `ClosedTrade`; `backtest/metrics.py` emits the
canonical `per_signal` block (net PnL, net R with the lot-sizing risk
denominator, t-stats ddof=1 via `statistics.stdev`) alongside unchanged
legacy per-leg fields; `backtest/walk_forward.py` PASS gate now reads
per-signal t (legacy fallback warns loudly). Tuple-key fallback for
pre-harness-v2 data mirrors `tools/ftmo_challenge_mc.py`. Change log:
`reports/phase0/diff_0.2_per_signal_stats.md`.

**Unit test result:** `tests/test_per_signal_metrics.py` — **3/3 PASS**
(hand-computed t/PF/E[R] fixtures; adversarial fallback case: two
opposite-direction signals sharing open_ts; legacy per-leg math unchanged).
Integration: both calibration reports show legs_covered == closed legs and
signals == legs/2 exactly (89×2=178, 77×2=154).

## 0.3 Calibration run

Pre-registered deliverable (verbatim):

> Calibration run: re-run the frozen legacy SMC config (MIN_SCORE=75,
> risk 0.65%, costs ON, market fill) on the SAME data as
> report_costs_065.json. Report old-vs-new PF and t side by side.
> EXPECTATION: numbers drop. Whatever the result, record it in
> lessons/ as the harness-v2 calibration anchor.

Provenance correction (evidence: `bt_costs_065.log` line
`fill_model=limit (ENTRY_AT_PROXIMAL=True)`; lesson 2026-05-30): the recorded
artifact used the **limit** fill model, not market. Calibration replicates the
artifact's actual config: limit fill, MIN_SCORE=75, risk 0.65%, costs ON,
HMM/DD-mod/time-stop OFF (they post-date the artifact), same CSV
(unchanged on disk since 2026-05-28; both legs replay the identical
165,237 bars). Because strategy code drifted after 2026-05-31, the
comparison uses two legs on identical current code differing ONLY in the
slice rule (V1 = legacy slicing, V2 = harness-v2), isolating the harness
effect from drift. V1-tree fidelity: diff vs main is hunk-identical to
`diff_0.1_lookahead_fix.patch`; V1 replay determinism verified at bar 54,200
against its crashed first attempt (state byte-identical;
crash evidence: `reports/phase0/bt_calib_v1_attempt1_crash.log`).

**Old-vs-new, side by side (PF and t as required):**

| | Recorded (`report_costs_065.json`) | V1 legacy slicing | V2 harness-v2 |
|---|---:|---:|---:|
| Per-LEG PF / t | 1.402 / +3.161 | 1.490 / +3.345 | **1.220 / +1.250** |
| Per-SIGNAL n / PF | 98 / — | 89 / 1.748 | **77 / 1.258** |
| Per-SIGNAL E[R] net | +0.308 | +0.346 | **+0.133** |
| Per-SIGNAL t (PnL / R) | — | +2.453 / +2.531 | **+0.917 / +0.928** |
| Return / MaxDD | +21.19% / 5.44% | +21.69% / 4.82% | **+6.67% / 5.43%** |

EXPECTATION "numbers drop": **CONFIRMED.** Harness effect alone (V1→V2):
per-signal t +2.453 → +0.917, E[R] −62%, PnL −69%, DD unchanged. Under the
honest harness the legacy config is statistically indistinguishable from zero
edge (n=77, t=+0.92 over 28.2 months). Full decomposition + generalizable
rule: `.claude/skills/quant-trading/lessons/2026-07-05-harness-v2-calibration-anchor.md`
(the calibration anchor required by the gate).

---

## Artifacts

| Artifact | Path |
|---|---|
| 0.1 diff (machine) | `reports/phase0/diff_0.1_lookahead_fix.patch` |
| 0.2 change log | `reports/phase0/diff_0.2_per_signal_stats.md` |
| Mandatory 0.1 unit test | `tests/test_no_lookahead.py` (1000/1000 PASS) |
| 0.2 unit test | `tests/test_per_signal_metrics.py` (3/3 PASS) |
| Calibration reports | `report_calib_v1_full.json`, `report_calib_v2_full.json` |
| Calibration logs | `bt_calib_v1.err.log`, `bt_calib_v2.err.log`, crash: `reports/phase0/bt_calib_v1_attempt1_crash.log` |
| Smoke evidence (v1==v2 on stub window, == artifact's first trade) | `report_calib_v1_smoke.json`, `report_calib_v2_smoke.json` |
| Calibration anchor lesson | `.claude/skills/quant-trading/lessons/2026-07-05-harness-v2-calibration-anchor.md` |
| V1 replica tree (kept for reviewer re-runs) | `E:\project_calib_v1_tree` (disposable after review) |
| Track A grader (parallel track, ready for posts CSV) | `tools/track_a_grader.py` + `tests/test_track_a_grader.py` (8/8 PASS) |

## Known limitations / open items (not G0 blockers)

1. **Repo has zero git commits** — no VCS baseline existed for any diff; 0.1
   is machine-diffed against the reconstructed v1 tree, 0.2 is an edit log.
   Recommend an initial commit (after .gitignore review — `.env` holds
   credentials) so Phase 1's "spec document committed" gate is meaningful.
2. **Live/backtest forming-bar gap** — live `copy_rates_from_pos(..., 0, N)`
   includes the partial forming bar; harness-v2 excludes it. Backtest is the
   conservative side. Reconciliation lands in Phase 3 (G3 ±25% realized-R
   check). Live code untouched in Phase 0.
3. Per-signal grouping across WFV window boundaries can split a signal's
   legs into adjacent windows (≤1 signal per boundary edge) — negligible at
   n≈100, documented in `walk_forward.py`.
4. V1 attempt-1 crash was a non-deterministic pandas C-level `SystemError`
   (attempt-2 reproduced attempt-1's trajectory exactly and completed) —
   environment flake, not a harness defect.

## Gate condition check

| Condition (verbatim) | Status |
|---|---|
| "unit test passes" | PASS — 1000/1000 (100%) |
| "per-signal stats implemented" | PASS — canonical in metrics + WFV gate; 3/3 tests |
| "calibration documented" | PASS — table above + anchor lesson |

**G0 PASS. Phase 1 (SPEC FREEZE) may begin.**
