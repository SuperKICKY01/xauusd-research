# Lesson 2026-06-13 — H1-pivot Phase 1: the limit thesis works mechanically but the constraint moved to signal frequency

**Date:** 2026-06-13 (investigation; backtests 2026-06-13).
**Status:** Phase 1 (Config B) complete. Spec + 3 sequential backtests done. **Config B
at expiry=4 FAILS the deploy gate on the honest bar** (t 1.98 < 2.5) and on FTMO
activity + consistency. No live change made (bot PID 6168 stayed on M5 market the whole time).
Config A (H4/H1/M15) deferred — flagged as pushing the *wrong* way on the new binding constraint.
**Window for the two costed runs:** full 11-mo OOS `data/xauusd_jan_nov2024_m5.csv`
(2024-01-01 → 2024-11-29), limit fill, costs ON (XM $7/lot RT + swap), risk 0.65%,
HMM ON, DD-mod ON, `MAX_CONCURRENT_TRADES=1`.

---

## 1. What we tested

**Thesis (from Fable):** the M5 market path's binding constraint was *anchor staleness* —
M5 signals re-fire at a stale OB and a market order chases it; planned-vs-live drift 3-40 pts,
RR gate then rejects ~9/10 fills. Resting **limit orders at the OB proximal edge** (the dormant
`EXECUTION_MODE=limit` / `pending_order_manager` system) should turn that drift into noise and
make the system viable.

**Config B** (chosen first, to isolate execution from a TF change): keep the live TF ladder
**H1/M15/M5**, flip only execution → `fill_model=limit`, `ENTRY_AT_PROXIMAL=True`. Apples-to-apples
vs the live M5 baseline except the entry mechanism.

**Load-bearing code fact found in spec:** the OB the limit rests on comes from the **SECONDARY**
slot (`signal_builder` `nearest_*_ob` on `smc_secondary`), *not* the ENTRY slot — "H1 entry"
maps to SECONDARY=H1, not the ENTRY TF.

---

## 2. Findings, in sequence (each step reframed the previous)

**STEP-1 — gate run, expiry=1 (default):** fill rate **13.6%** (9/66 placed), 18 legs/9 fills,
WR 66.7%, PF 10.42, raw t 4.66 (honest ~3.3), MaxDD 1.14%, +7.79%. *Looked like the same ~1/10
starvation as M5* — but via limits **expiring unfilled**, not RR-rejection. Per the pre-committed
"poor fill rate → thesis dead" rule this leaned STOP — except the 9 fills were excellent
(real-but-rare).

**STEP-2 — expiry sweep (diagnostic, retrace ceiling):** rather than re-run the 5h pipeline ×4,
recognized **expiry doesn't change signal generation** → ran the SMC pipeline ONCE, swept expiry
as a post-process (`tools/_expiry_fillrate_sweep.py`, concurrency-independent retrace fraction,
HMM off, 4-mo window, ~66 min). Result (n=31 distinct OB limits):

| expiry (H1) | 1 | 4 | 8 | 12 |
|---|---|---|---|---|
| retrace fraction | 48.4% | 67.7% | 67.7% | 74.2% |

26% never retrace within 13 candles; the lift lands by expiry=4. **Expiry, not "price never
retraces," was the artifact behind STEP-1's 13.6%.**

**STEP-3 — costed gate run, expiry=4** (else identical to STEP-1):

| | STEP-1 (exp=1) | STEP-3 (exp=4) |
|---|---|---|
| Real fill rate | 13.6% (9/66) | **27.9% (12/43)** |
| WR (leg) | 66.7% | 50.0% |
| Profit factor | 10.42 | 4.88 |
| Avg R realised | 1.41 | 1.27 |
| t (raw, leg-n) | 4.66 | 2.82 |
| **t (honest, 12 fills)** | ~3.3 | **1.98** |
| Return / MaxDD | +7.79% / 1.14% | +8.88% / 1.34% |

Fill rate **doubled** (lever confirmed; real 27.9% sat ~3.5× below the 67.7% ceiling, as predicted).
But the 3 marginal fills **diluted quality**: WR, PF, avg R and t all fell. Honest t (legs collapsed
to 12 independent fills) = **1.98 < 2.5 → gate FAIL**. FTMO: **activity ~1.1 fills/month** (≈1 trade
per 30-day challenge) and **profit concentration** — best day 59.4% of net, single best fill $3,936 =
**44%** of net, top-3 days 87% → fails consistency rules.

---

## 3. Root finding — the binding constraint moved

Execution was the *old* constraint and we addressed it: limits fill clean at the proximal edge
(no entry slippage, no RR-gate rejection). What's left exposed is the **signal**: the SMC boolean
confluence score yields **~1 quality OB-retest per month**, and stretching expiry only buys
*quantity at quality's expense*. Execution fixes cannot rescue a low-frequency, outlier-dependent
signal.

This **converges with the external reviews:** Fable ("alpha never showed live edge") and Gemini
("SMC is the narrative; the real core is order-flow imbalance — it needs a probability layer, not a
boolean gate"). All three lines now point at signal design, not execution.

---

## 4. Generalizable rules

- **(a) When adding samples LOWERS significance, the marginal samples are noise.** STEP-1→3: n
  rose 9→12 fills but honest t fell 3.3→1.98. The edge lived in a thin subset; the extra fills
  diluted it. A larger n that *reduces* t is a tell that you're harvesting noise, not edge.
- **(b) Fixing execution cannot fix a signal-frequency problem.** Once realized fills match planned
  geometry, the ceiling is set by how many quality setups the signal emits. Diagnose *which* layer
  binds before optimizing a layer.
- **(c) Optimistic ceilings overstate realized rates — always distinguish them.** The
  concurrency-independent retrace fraction (67.7%) overstated the real fill rate (27.9%) by ~3.5×.
  A ceiling is a go/no-go *direction* signal, never the deployable number.
- **(d) Profit concentration is fragility, not edge.** One fill = 44% of net; remove it and 11-mo
  return halves and t collapses. Outlier dependence fails prop consistency rules and won't replay.

**Reusable technique:** compute the honest t-stat by collapsing each dual-TP fill's TP1/TP2 legs
into one trade (legs share entry/SL/direction → correlated); `t = mean/std·√n` on the n *fills*,
not the 2n legs. Leg-n t overstated significance by ~1.4× here (2.82 vs 1.98).

---

## Code / artifacts from this phase

- **`quant/signal_builder.py`** — Fix-2 price-distance filter gated on `ENTRY_AT_PROXIMAL`
  (skip in limit geometry; a resting limit is *meant* to sit away from price). Gated on the geometry
  flag, NOT `CFG.execution.mode`, because that's the flag `strategy_runner` flips per `fill_model` —
  market mode stays byte-identical; the backtest's limit path gets the gate. **Market path unchanged;
  live bot untouched.**
- **`tools/_expiry_fillrate_sweep.py`** — report-only retrace-fraction diagnostic (collect signals
  once, sweep expiry as post-process). Reusable for any "does waiting longer help fill" question.
- **`tools/_configB_oos.json`** (exp=1), **`tools/_configB_exp4.json`** (exp=4), `tools/_expiry_sweep.json`.

## Related
[[2026-06-07-slippage-cliff-and-audit]] (deferred the execution pivot to live evidence; this phase
ran the backtest framing instead) · [[2026-05-29-pending-limit-order-system]] (the dormant limit
system this re-tested) · [[2026-06-05-llm-htf-demotion]]

## Promotion candidates (per protocol — NOT yet promoted)
- Rule (a) and the leg-collapse t technique are strong SKILL.md candidates (general statistical
  hygiene). Hold for one more instance before promoting, per protocol.
