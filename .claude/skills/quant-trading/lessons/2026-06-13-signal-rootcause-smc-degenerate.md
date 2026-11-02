# Lesson 2026-06-13 — Signal root-cause: the SMC confluence score is degenerate (~1 real dimension), and its one varying feature overfits

**Date:** 2026-06-13 (read-only diagnosis; no backtest, no code, bot PID 6168 untouched).
**Status:** Decisive. Follows [[2026-06-13-h1-limit-phase1]], which proved execution was the
*symptom* (limit + expiry fixes don't create edge) and relocated the binding constraint to
**signal design**. This lesson diagnoses the signal itself and finds the base is the problem.
**Corrects** the earlier in-sample ≥2-timing-flag result (STEP-3b, t +2.18→+3.05): it does NOT
survive out-of-sample.
**Method:** ran the existing `backtest/_flag_analysis.py` on existing flag-instrumented reports
+ decomposed score↔flag variance within the live (≥75) cohort. Read-only throughout.

---

## 1. What we asked

After Phase 1 showed the binding constraint is signal frequency/quality (not execution), the
gate question before building any meta-label / probability layer: **does the SMC flag base even
carry learnable, out-of-sample edge?** Three sub-questions: per-flag predictive power; sample
sufficiency for ML; honest verdict (foundation, or is the base the problem).

---

## 2. Finding 1 — the "6-factor score" is ~1 real dimension

The score is a fixed sum of `{EMA 20, BOS 20, OB 20, CHoCH 15, FVG 15, align 10}`, MIN_SCORE 75.
Decomposing every achievable ≥75 score against the actual in-sample histogram (514 signals /
1,028 legs, `report_full.json`, 2024-12 → 2026-05) gives **how often each flag actually varies
within the traded cohort**. A flag that is almost always on (or off) cannot separate winners
from losers — there is no contrast to learn from:

| Flag | % ON in ≥75 cohort | Discriminating power |
|---|---|---|
| EMA (20) | ~98% (≈always on) | ~none — constant entry gate |
| BOS (20) | ~98% (≈always on) | ~none — **collinear with OB by construction** (OB = candle before the BOS impulse → 40/100 pts double-counted, per [[2026-06-07-slippage-cliff-and-audit]]) |
| OB (20) | ~98% (off only in the 8 score-80 sigs) | ~none — near-constant |
| Alignment (10) | ~7% (≈always off) | negligible |
| **CHoCH+FVG (timing count)** | 1-flag 55.6% / 2-flag 44.4% | the **only** feature with real variance |

**5 of 6 flags are near-constant within the published cohort — entry gates, not discriminators.**
The composite's apparent richness is an illusion: it is effectively a single binary dimension
(timing-flag count) plus four constants. This is a structural fact of the scoring arithmetic —
it holds independent of sample size.

---

## 3. Finding 2 — the only varying feature OVERFITS

The one dimension with variance (timing-flag count) was the basis of the earlier STEP-3b
"≥2-timing-flags is better" result. Tested in- vs out-of-sample:

| | dataset | ≥2-timing-flag | read |
|---|---|---|---|
| In-sample | `report_full` ≥75, 1,028 legs | t **+3.05** | looked like edge |
| Out-of-sample | window reports ≥75, **86 legs** | t **+0.09**, PF **0.98** | **edge gone** |

(Whole ≥75 OOS cohort: t +0.87, PF 1.24.) **The in-sample edge collapses out-of-sample** — the
textbook overfitting signature. We have, in effect, already run the meta-feature experiment on
the one informative-looking feature, and it failed OOS. **This supersedes the +3.05 finding;
record it honestly.**

---

## 4. Sample sufficiency

- In-sample ≥75: 514 signals / 1,028 legs over ~17 months (~30 published/mo) — raw count sounds
  adequate.
- But: **feature space ≈ 1 binary dimension** (nothing to learn *from*); **high-conviction tail
  is tiny** (score 85+100 = 30 signals total, ~1.8/mo — the sample-size trap from
  [[2026-06-07-slippage-cliff-and-audit]]); **OOS folds are un-validatably thin** (window
  reports hold 2–22 signals each, 43 pooled); heavy **re-fire clustering** (e.g. 200 sigs at
  score 90) inflates the count above the number of independent setups.
- Conclusion: not the raw count that kills ML feasibility — the **degenerate feature space +
  un-validatable OOS folds** do. A model here fits ~1 noisy binary feature and overfits (as §3
  already demonstrates).

---

## 5. Verdict + direction

**The SMC flag base is NOT a foundation worth a meta-label / probability layer.** It is ~1 noisy
binary feature; building ML on it = fitting noise. This **converges with all independent reads**:
Fable ("alpha never showed live edge"), Gemini ("SMC is narrative; the real core is order-flow
imbalance — needs a probability layer, not a boolean gate"), and the 2026-06-07 self-audit
("6 summed booleans, OB+BOS double-count, score isn't calibration").

**The root is signal/feature design:** the next foundation needs a feature with genuine
*continuous variance* and *OOS-stable* predictive content — order-flow imbalance (OFI) /
microstructure — fed to a probability model, not more processing of boolean SMC flags.

**Hard constraint (data ceiling):** retail MT5 exposes only L1 tick / partial L2 depth, **no L3
order-by-order** flow. A true OFI rebuild is therefore very hard on this data stack — any
order-flow proxy will be a degraded approximation, and this cost must be weighed against the
alternative (e.g. freezing the current system as a small, capped-risk portfolio component). That
strategic call (rebuild-on-OFI vs freeze-as-portfolio) is a human decision, not a code task.

---

## Generalizable rule

**A multi-factor score whose factors are near-constant within the *traded* cohort is secretly
one-dimensional.** Always decompose factor variance *within the live (post-threshold) population*,
not over all candidates — a factor that gates entry (always on once you've filtered) contributes
zero discrimination among the trades you actually take. Validate any composite-score edge on a
factor-variance breakdown + OOS before trusting it, and treat an in-sample factor edge that
vanishes OOS as overfit, not as a tuning opportunity.

---

## Data / artifacts (read-only)

- `report_full.json` — in-sample full history (2024-12→2026-05); ≥75 cohort = 1,028 legs / 514
  signals; score histogram `{75:552, 80:16, 85:20, 90:400, 100:40}` (legs).
- `report_w1..w5.json`, `report_prefix/postfix.json` — flag-instrumented window reports
  (carry `timing_flag_count`); pooled ≥75 = 86 legs (the OOS check in §3).
- `backtest/_flag_analysis.py` — the existing tool used (STEP 3a CHoCH/FVG split, STEP 3b
  timing-count counterfactual).

## Related
[[2026-06-13-h1-limit-phase1]] (execution = symptom; this = the root) ·
[[2026-06-07-slippage-cliff-and-audit]] (self-audit: score = 6 booleans, OB+BOS double-count,
score ≠ calibration; the +3.05 STEP-3b result this corrects originated in that flag-instrumentation work) ·
[[2026-05-29-walk-forward-results]] (edge real but thin; trade volume the constraint)

## Promotion candidate (per protocol — NOT yet promoted)
- The generalizable rule (decompose factor variance *within the traded cohort*; secretly-1D
  scores) is a strong SKILL.md candidate — general modelling hygiene beyond this project. Hold
  for one more instance per protocol, or promote now if it recurs.
