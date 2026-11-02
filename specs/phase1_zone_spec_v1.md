# PHASE 1 — SPEC FREEZE: Supply/Demand Zone Strategy v1.0 (XAUUSD)

Project: Zone-Strategy Falsification Pipeline (XAUUSD) — Master Spec v1.0
Role: BUILDER (Claude). Date: 2026-07-14. Status: **FROZEN — pending user review.
No backtest has touched any data (DEV or EVAL) as of this freeze.**

Phase/Gate addressed: **Phase 1 (1.1, 1.2, 1.3), Gate G1.**

Gate text (quoted verbatim from the Master Spec):

> GATE G1: spec document committed; partitions declared; ledger initialized.

Hypothesis under test (Master Spec §0, verbatim):

> New hypothesis under test: a supply/demand ZONE strategy (draw zones in
> advance, trade reversion/reaction at zone touch, both directions,
> TP ladder + trailed stop). This is in the same falsified FAMILY as SMC
> order blocks. Prior P(durable edge) is LOW (~3-5%). The pipeline's job is
> to give this hypothesis one honest, cheap, pre-registered chance.

Prior probability of a durable edge is pre-registered at **~3–5%**. The
expected outcome of this pipeline is FALSIFICATION; a pass must clear every
pre-registered gate below with no post-hoc adjustment.

---

## 1.1 Strategy specification (ONE variant, all definitions filled)

All parameters below are **frozen v1.0 values**. Any change to any value
constitutes a new trial variant: it MUST be logged as a new row in
`trials.csv` (§1.3) and may only be evaluated on the DEV partition (§1.2).
The EVAL partitions see exactly ONE configuration: this one.

### 1.1.1 Timeframes

| Role | Timeframe |
|---|---|
| Zone construction + invalidation + ATR | H1 (closed bars only, harness-v2 slicing) |
| Execution (touch detection, entry, exits) | M5 |
| M15 | **unused** |

All H1 quantities at any M5 decision time use only H1 bars whose **close
time ≤ decision time** (harness-v2 `slice_window`, anti-pattern #9). Zones
are drawable in advance by construction: every zone's geometry and SL are
fixed at zone activation time, before any touch.

### 1.1.2 Indicator

- **ATR(H1, 14)** — Wilder smoothing (RMA) of true range on H1 closed bars.
  Written `ATR` below. Where a zone rule references ATR, the value is taken
  **at the zone's activation bar** and frozen onto the zone (no drift).

### 1.1.3 Zone construction (exact)

**Swing definition (2-bar fractal, H1):**
- Swing HIGH at bar `i`: `high[i] > high[i−1], high[i−2], high[i+1], high[i+2]`
  (strict inequalities). Detectable only once bar `i+2` has closed.
- Swing LOW: mirror with lows.

**Impulse confirmation (departure leg):** the swing spawns a candidate zone
only if within the 3 H1 bars after the swing bar (`i+1 … i+3`) at least one
bar CLOSES at a distance ≥ **1.5 × ATR[i]** from the swing extreme, on the
side away from the zone:
- Supply (from swing high): some `close[k] ≤ high[i] − 1.5×ATR[i]`, k ∈ {i+1..i+3}.
- Demand (from swing low): some `close[k] ≥ low[i] + 1.5×ATR[i]`.

**Zone activation time** = close time of the first confirming bar `k` (also
requires `k ≥ i+2` implicitly satisfied — fractal needs `i+2` closed). No
zone exists, and no touch counts, before this time.

**Zone geometry** (from the swing bar `i` only):
- Supply: distal (top) = `high[i]`; proximal (bottom) = `max(open[i], close[i])`.
- Demand: distal (bottom) = `low[i]`; proximal (top) = `min(open[i], close[i])`.

**Degenerate/size guards** (checked at activation, zone discarded if violated):
- Minimum height: `(distal − proximal) ≥ 0.05 × ATR[i]` (kills doji zones).
- Maximum height: `(distal − proximal) ≤ 2.0 × ATR[i]` (kills pin-bar monsters).

**Zone lifecycle:**
- **Max age: 72 H1 bars** from activation. Untouched after 72 bars → expired.
- **Invalidation (before touch):** any H1 CLOSE beyond the distal edge
  (supply: `close > top`; demand: `close < bottom`) → zone dead.
- **One trade per zone / first touch only:** the first M5 touch of the
  proximal edge CONSUMES the zone permanently — whether the resulting trade
  fills, is skipped by a guard, or is vetoed. No re-entry on re-touch.
- **Overlap rule:** if a newly activated zone overlaps in price with an
  existing active same-direction zone, the NEWER zone replaces the older
  (freshness); the older is removed unconsumed.

### 1.1.4 Entry — **touch-entry** (the ONE picked; confirmation-entry rejected)

Master Spec 1.1 requires picking ONE of touch-entry / confirmation-entry.
**Picked: touch-entry.** Fill mechanics are market-fill per Master Spec 1.1
("costs ON, market-fill"); the limit-fill model is explicitly NOT used
(known to preserve RR by construction — see
`lessons/… rr-gate` / memory `rr-rejection-structural`, an optimistic artifact).

- **Touch (M5):** supply → `high[m5] ≥ proximal`; demand → `low[m5] ≤ proximal`,
  while the zone is active, unconsumed, unexpired, uninvalidated.
- **Fill:** market order at the **next M5 bar's open** with **0.25 adverse
  slippage** (25 points = 2.5 pips, the existing frozen slippage penalty):
  short fill = `open − 0.25`; long fill = `open + 0.25`.
- **Gap handling:** if the next M5 open is at/beyond the SL level or beyond
  the distal edge → **no trade**, zone consumed, logged as `skipped_gap`.
  If the open is merely deeper inside the zone → fill anyway at open ±
  slippage (worse entry, same structural SL).
- **Degenerate-fill guard:** if `|fill − SL| < 0.05` (5 points) → no trade,
  zone consumed, logged as `skipped_degenerate`.
- Both directions traded. **No session filter. No news filter in backtest**
  (live chassis keeps its news veto in Phase 3 — declared reconciliation item).

### 1.1.5 Stop loss (structural, never re-anchored)

> SL: fixed at distal edge + 0.2*ATR(H1). No trade without SL. (Master Spec 1.1)

- Supply short: `SL = top + 0.2 × ATR[activation]`.
- Demand long: `SL = bottom − 0.2 × ATR[activation]`.
- SL is a property of the ZONE, computed once at activation. It is **not**
  re-anchored to the fill price (re-anchoring is the known RR-preserving
  backtest artifact). Every trade has an SL by construction.

### 1.1.6 TP ladder + trailing rule (declared)

**R** ≡ `|fill − SL|` per trade (actual fill, structural SL).

Two legs, equal size (the existing dual-TP broker machinery):
- **Leg 1 (50%): TP1 = fill ∓ 1.0 × R** (1R).
- **Leg 2 (50%): TP2 = fill ∓ 2.0 × R** (2R).
- **Trailing rule: breakeven-after-TP1** — when leg 1 closes at TP1, leg 2's
  SL moves to the fill price. No other trailing.

Declared deviation: Master Spec 1.1 gives "e.g., TP1/TP2/TP3 at 1R/2R/3R" as
an example. v1.0 uses a 2-level ladder (1R/2R + BE) because the verified
Phase-0 fill simulator is dual-TP; adding a third leg would require new
broker machinery inside the frozen harness. This is a pre-registered choice,
not a tuned one.

### 1.1.7 Risk, concurrency, costs (frozen)

| Item | Frozen value |
|---|---|
| Risk per signal | **0.5%** of balance, split 50/50 across the two legs |
| Lot sizing | existing `VirtualBroker._calculate_lot`, SL distance includes the slippage buffer (anti-pattern #4) |
| DD-modulated sizing | **OFF** |
| HMM regime veto | **OFF** |
| Macro bias | **OFF** |
| Time stop | **OFF** |
| Max concurrent | **1 signal** (= 2 legs); while any leg is open, no new touch may trigger an entry |
| Fill model | **market** (harness-v2 verified path) |
| Costs | **ON**: $7/lot RT commission; swap −3.75/−5.49 per lot/night, Wednesday 3×; 0.25 adverse entry slippage |
| Intra-bar convention | existing pessimistic rules unchanged: both SL and TP inside one bar → SL first; gap-through → close at bar open |

### 1.1.8 Harness requirements (inherited from Phase 0, non-negotiable)

- Harness-v2 only: `slice_window(..., bar_minutes)` close-time slicing
  (`tests/test_no_lookahead.py` must pass before any run is reported).
- All gate statistics **per-signal** (`metrics per_signal`,
  `tests/test_per_signal_metrics.py`), never per-leg.
- Zone state at any decision time must be reconstructible from closed H1
  bars only — the implementation must expose a replay-deterministic zone set
  (same input CSV ⇒ byte-identical trade list).

---

## 1.2 Data partition (declared BEFORE running)

Master Spec 1.2, verbatim:

> - DEV window (tuning allowed): 2024 only.
> - EVAL windows (untouchable until spec frozen): 2025-H1, 2025-H2,
>   2026-01..06. Note: these overlap the SPENT W1-W3 — therefore also
>   reserve FRESH OOS = all data after 2026-06 (collect forward).
>   Primary evidence = EVAL under harness-v2 + FRESH OOS.

Declared concretely against the data on disk (`data/xauusd_m5_full.csv`,
165,437 rows, 2024-01-01 23:00 → 2026-05-06 02:35, unchanged since
2026-05-28):

| Partition | Range (bar open times) | Status |
|---|---|---|
| DEV | 2024-01-01 → 2024-12-31 | tuning allowed; every run logged in `trials.csv` |
| EVAL-1 | 2025-01-01 → 2025-06-30 | ONE run with the frozen spec, in Phase 2 only |
| EVAL-2 | 2025-07-01 → 2025-12-31 | ONE run with the frozen spec, in Phase 2 only |
| EVAL-3 | 2026-01-01 → 2026-06-30 | ONE run; **currently truncated at 2026-05-06 by data end** — refresh the CSV before the Phase-2 EVAL pass and record the refreshed file's hash in the Phase-2 report |
| FRESH OOS | all data after 2026-06-30 | collect forward; primary evidence together with EVAL |

Contamination disclosures (pre-registered):
1. W1 (2025-H1), W2 (2025-H2), W3 (2026→05-31) are **SPENT** on the old SMC
   strategy. They overlap EVAL-1/2/3. Per Master Spec §0 they "may be
   reported as sanity checks only"; the FRESH OOS reserve exists because of
   this overlap. No parameter of §1.1 was chosen by looking at 2025–2026
   XAUUSD data in this pipeline.
2. Warmup: each run consumes ≈8 trading days of H1 lookback at the start of
   its window before the first signal can exist (measured in Phase 0).
   Window boundaries above are data boundaries, not signal boundaries.
3. If any EVAL window is ever run more than once with differing configs,
   that window is burnt and must be disclosed as such in every subsequent
   report; the respec rule (Master Spec §4) then applies:
   > Any respec = return to Phase 1 with a NEW untouched eval partition (in
   > practice: wait for fresh forward data; the calendar is the budget).

Pre-registered Phase-2 success criteria (Gate G2, verbatim, so they cannot
drift):

> GATE G2 (all required, pre-registered):
>   - Per-signal t >= 2.5 in at least 2 of 3 EVAL windows, AND pooled
>     per-signal t >= 2.5 across EVAL.
>   - Net expectancy >= +0.15R after costs in the pooled EVAL set.
>   - Canonical DSR > 0.95 at N = ledger count (not N=12).
>   - Max DD (bar-close) <= 6% at 0.5% risk.

---

## 1.3 Trial ledger

`trials.csv` at the repo root, initialized header-only at this freeze.
Master Spec 1.3, verbatim:

> TRIAL LEDGER: a file (trials.csv) logging EVERY config variant ever
> backtested (params + result). The ledger count N feeds the DSR
> computation. Deleting rows is forbidden.

Rules of use:
- **Append-only. Rows may never be deleted or edited.** Corrections are new
  rows with a `supersedes=<trial_id>` note.
- A row is appended for EVERY backtest of a zone-strategy config on ANY
  partition — including failed runs, crashed runs (result fields blank,
  note the crash), and DEV tuning throwaways. If it ran, it counts.
- `N` for the DSR computation (`tools/deflated_sharpe.py`) = number of rows
  (distinct trial_ids) at the time of the Phase-2 report.
- Columns: `trial_id, timestamp_utc, phase, partition, spec_version,
  params_json, fill_model, risk_pct, costs_on, n_signals,
  expectancy_r_per_signal, t_stat_per_signal, pf_per_signal, max_dd_pct,
  notes` — statistics columns are per-signal (canonical) only.

Ledger state at freeze: **0 rows** (header only). No backtest of this
strategy has been run on any partition.

---

## G1 condition check

| Condition (verbatim) | Status |
|---|---|
| "spec document committed" | this file, committed in the fresh E:\project repo |
| "partitions declared" | §1.2, declared before any run |
| "ledger initialized" | `trials.csv`, header-only, 0 rows |

**FROZEN. Awaiting user review. No backtest (DEV or EVAL) starts before
review sign-off.** Implementation of the zone engine (Phase-2 builder work)
also waits, per the review instruction.

---

## Appendix A — Parameter provenance (reviewer item, 2026-07-15; additive only)

No backtest, scan, plot, or statistic has been computed on any partition for
this strategy (DEV included). Data access before the freeze: `head/tail/wc`
on the CSV for §1.2 boundary declaration only. Provenance by category:

**A — mandated by the Master Spec (not chosen):** SL = distal + 0.2×ATR(H1);
risk 0.5%; DD-mod OFF; costs ON; market-fill; max concurrent 1. The
touch/confirmation dichotomy required a pick: touch-entry, chosen a priori
because it is the variant the source hypothesis describes ("trade
reaction at zone touch").

**B — a-priori generic defaults, zero data consulted, never iterated:**
2-bar fractal (Bill Williams convention); impulse ≥1.5×ATR within 3 bars
(generic "strong departure leg" doctrine); wick-to-body zone geometry
(standard S/D drawing); 72-bar age (~3 trading days freshness default);
0.05×/2.0×ATR height guards (degeneracy protection, not performance).
DEV tuning of these is clean; every variant = a `trials.csv` row.

**C — informed by this project's PRIOR data-derived findings (disclosed
contamination):** (1) market fill + never re-anchor SL ← the 2026-06-21
RR-rejection audit on live demo fills (2026 data, overlaps EVAL-3/FRESH);
(2) the 2-leg 1R/2R + BE ladder ← inherited from the dual-TP machinery of
the SMC era (built on 2024–2026-05 data; the ladder was a design constant,
not swept, but is not virgin); (3) 0.25 adverse slippage ← calibrated
2026-05 from live slippage telemetry. All three are execution-realism
choices, not entry-signal parameters, and each biases the backtest AGAINST
the strategy — nonetheless they are Category C and are pre-registered as
**fixed infrastructure, NOT tunable on DEV** (re-tuning fill/cost realism
against DEV would launder execution assumptions into alpha).

**Residual prior (cannot be purged):** the builder model's training data
(cutoff 2026-01) contains gold's 2024–2025 price history in aggregate. No
number was set using it, but a perfectly virgin prior is unfalsifiable.
Mitigation is structural: FRESH OOS (post-2026-06, collected forward) is
virgin w.r.t. both the model's training data and this project's history —
which is why it is primary evidence alongside EVAL.
