# Lesson 2026-05-31 — HMM Regime Detection (GaussianHMM) as a pre-signal filter

**Status:** Implemented, smoke-tested, and **WFV-validated** (2026-06-02). Config was frozen
before looking at the test windows. **Pre-registered criterion (S↑ in W1+W2, W3 no regress)
PASSED on all three windows.** BUT the t>2.5 deployment gate still fails in every window
(`wfv_hmm_report.json` all_passed=false) and W1 is still net-negative — passing the criterion
is NOT a go-live signal. Results below.

## RESULT (cost-aware, limit geometry) — HMM vs `wfv_baseline_75.json`

| Window | Trades B→H | PF B→H | t-stat B→H | per-trade Sharpe B→H | ΔSharpe |
|---|---:|---:|---:|---:|---:|
| W1 | 48→44 | 0.73→0.85 | −0.79→−0.30 | −0.114→−0.045 | +0.069 ✅ |
| W2 | 58→46 | 1.29→1.41 | 1.45→1.71 | 0.190→0.252 | +0.062 ✅ |
| W3 | 52→52 | 1.62→1.63 | 2.20→2.22 | 0.305→0.307 | +0.002 ✅ flat |

Read: **W2 is the real win** — the veto cut 21% of trades yet Sharpe rose 33% AND t rose,
i.e. it beat the √n penalty by clearing below-average trades. **W3 untouched** (52→52): the
filter was correctly inert in a genuinely trending regime — the ideal behaviour. **W1 improved
but still loses** ($−811, PF 0.85); its 75–84 score bucket still bleeds (32 trades, 25% WR,
−$1,055) — HMM did not fix that, so the 75–84 bucket is the next thing to investigate (as a
SEPARATE question, on fresh OOS data — the W1/W2/W3 windows are now spent for tuning).

The coherent pattern (choppy windows improved, clean trend left alone) is encouraging but the
samples are small (n≈44–52); one strong window + one directional + one neutral, not a slam dunk.

## What we built

A 3-state `GaussianHMM` (`hmmlearn`) regime detector on the PRIMARY (H1) timeframe,
wired into `SignalBuilder` as a pre-signal gate. Features: `[log return, ATR/close]`.
Binary veto policy (2026-05-31 decision): **RANGING → veto, VOLATILE → pass through**
(revisit VOLATILE with evidence later). Files: `quant/hmm_regime.py` (new), edits to
`quant/signal_builder.py`, `config.py` (`HMMConfig`), `backtest/strategy_runner.py`
(per-run reset), `requirements.txt` (`hmmlearn>=0.3.0`). The broker was deliberately
**not** touched — under a binary 0/1 regime factor the veto in SignalBuilder is
sufficient; a regime-factor multiply in the broker would be redundant or double-count.

## Why this is a per-trade-Sharpe experiment, not a "make-W2-pass" exercise

The gate metric is `t = (mean_pnl / sd_pnl) × √n = S × √n` (`backtest/metrics.py`). A veto
only *removes* trades, shrinking `√n`. It raises t **only if** it lifts per-trade Sharpe
`S` faster than it shrinks `√n`: `S_new/S_old > √(n_old/n_new)`. The 2026-05-29 WFV lesson
showed W2 needs ~526 trades for t=2.5 and has far fewer; a veto pushes the count *down*.
So "W2 passes t>2.5" is the wrong target and inviting it would be Goodhart fitting on the
validation set (the thing [[2026-05-29-walk-forward-results]] explicitly forbids). **Success
= per-trade Sharpe rises in W1 AND W2 with W3 not regressing**, config frozen beforehand.

## Cost-aware baseline established first (MIN_SCORE=75, costs ON, limit fill)

`wfv_baseline_75.json` (added `--costs` flag to `backtest/walk_forward.py`):

| Window | Trades | PF | t-stat | per-trade Sharpe |
|---|---:|---:|---:|---:|
| W1 | 48 | 0.73 | **−0.79** | −0.114 |
| W2 | 58 | 1.29 | **1.45** | 0.190 |
| W3 | 52 | 1.62 | **2.20** | 0.305 |

Note vs the 2026-05-29 numbers (MIN_SCORE=65, cost-free): trade counts ~halved, **W1 went
net-negative**, W3 fell below 2.5. Two variables moved (MIN_SCORE 65→75 AND costs on), so
not cleanly attributable — but W1's negative edge makes it the highest-upside target for a
RANGING veto (room to cut losers). Also: WFV runs LIMIT geometry, not live MARKET — see
[[wfv-fill-model-limit]] (memory).

## Design decisions (frozen a priori)

- **Features normalized:** `ATR/close` not raw ATR (gold ranges $1,800→$2,400; raw ATR is
  non-stationary). Features z-scored with the training window's mean/std, stored with the model.
- **Labeling uses |mean|, not raw mean:** VOLATILE = highest return-variance state; of the rest,
  higher |mean return| = TRENDING, lower = RANGING. Raw "high mean" (the original spec) would
  misclassify strong DOWN-trends as not-trending. Relabel every refit (HMM state indices switch).
- **Lookahead safety:** detector buffers only CLOSED bars (drops the forming last bar of each
  window); fit/predict run only on buffered past bars; classify returns the last bar's Viterbi
  state. Reset per run in `strategy_runner` so windows don't leak. Detector update() runs BEFORE
  the EMA-RANGING early-return so the closed-bar buffer stays continuous.
- **Determinism:** pinned `random_state=42` (GaussianHMM uses random init → WFV subprocesses
  would otherwise be irreproducible).
- **Fail-open:** disabled / warmup / fit failure → UNKNOWN → no veto (SKILL anti-pattern #5).
- **Defaults:** n_components=3, train_window=1000, retrain_interval=500, min_warmup=300,
  covariance=diag, n_iter=100. Chosen on principle, NOT tuned to test windows. `HMM_ENABLED`
  defaults False so baseline/live are untouched until explicitly enabled.

## Smoke-test findings (synthetic data)

Mechanics all pass: fits, learns 3 distinct labels, buffer dedups by timestamp, drops the
forming bar, integration runs end-to-end, deterministic. Discrimination on a synthetic
trend/range/volatile concatenation (single representative fit): **VOLATILE 247/249 correct**,
but **trend-vs-range only ~52% / ~52% majority** — near coin-flip at the bar level. The
discriminating signal between trend and range (persistent drift) is subtle per-bar. So the
RANGING veto may be NOISY. Real H1 gold may differ; the WFV is the real test.

## Backtest gate (next step)

```
HMM_ENABLED=true python -m backtest.walk_forward data/xauusd_m5_full.csv \
  --risk-pct 0.65 --t-stat-threshold 2.5 --costs --report-json wfv_hmm_report.json
```
Compare per-window per-trade Sharpe vs `wfv_baseline_75.json`. Pass = S↑ in W1+W2, W3 no regress.

## Generalizable rule

**A regime/quality VETO must be justified against `t = S×√n`, not trade count alone: removing
trades only helps the t-stat if it lifts per-trade Sharpe faster than it shrinks √n.** And:
tuning a new filter's hyperparameters against the same WFV windows you report on is in-sample
fitting on the validation set — freeze the config on first principles (or synthetic data),
then evaluate once.

## Related
- [[2026-05-29-walk-forward-results]] — the t=S√n math, Goodhart warning, "don't change RANGING-filter behaviour."
- [[wfv-fill-model-limit]] — WFV uses limit geometry, not live market.
