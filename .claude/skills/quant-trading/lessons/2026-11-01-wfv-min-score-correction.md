# Correction 2026-11-01 — the 2026-05-29 walk-forward ran at MIN_SCORE=75, not 65

**Type:** Correction note. The original lesson
[[2026-05-29-walk-forward-results]] is left unchanged as the historical record.
This note supersedes one statement in it.

## The statement being corrected

`2026-05-29-walk-forward-results.md` L5 says the configuration under test was
"`MIN_SCORE=65`, RANGING filter active, …".

## Evidence that the effective threshold was 75

1. **Score buckets in the artifact.** `wfv_report.json` → `windows[].metrics.score_buckets`
   contains only `75-84` and `85-100`. `backtest/metrics.py::_bucket` is data-driven:
   a trade scoring 65–74 would land in a `65-74` bucket. The bucket counts sum to the
   window totals (W1 62+18=80, W2 68+20=88, W3 82+10=92), so **zero trades below 75**
   were taken in any window.
2. **Dated configuration history.** `2026-05-25-ob-invalidation-fix.md` (evidence dated
   2026-05-26) records `MIN_SCORE` raised 65 → 75 in `quant/signal_builder.py`. The next
   lessons (2026-05-27 and 2026-05-28) state MIN_SCORE=75 as the production config.
   The WFV launched 2026-05-28 22:28 (`wfv_run.log` L1), after the raise.
3. `quant/signal_builder.py:409` today: `MIN_SCORE = 75  # Raised 65→75 on 2026-05-26`.

## Limits of this correction

The per-window child logs that would show the runtime config (`wfv_logs/wfv_W{1,2,3}.log`)
were overwritten by a later run on 2026-06-18. The conclusion rests on (1), which is
direct artifact evidence, plus (2) and (3) as corroboration. The other parameters in the
original lesson (risk 0.65%, slippage buffer 2.5 pip, RANGING filter) were not re-checked
by this note.

## Effect on downstream claims

None of the WFV numbers change; only the stated configuration label does. Anyone citing
`wfv_report.json` should describe it as MIN_SCORE=75 (legacy harness, per-leg t-stats).
