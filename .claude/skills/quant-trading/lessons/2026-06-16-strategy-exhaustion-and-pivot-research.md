# Lesson 2026-06-16 — Strategy search exhausted on retail-gold price prediction; cointegration also fails OOS — chassis confirmed as the asset

**Date:** 2026-06-16. Read-only research session; bot 6168 (live gold-SMC) untouched throughout — no MT5, no config change. **$0 spend** (free data only: yfinance daily, Binance public REST).
**Status:** Decisive / strategic. This lesson caps the falsification arc that runs through
[[2026-06-13-signal-rootcause-smc-degenerate]] and [[2026-06-15-ftmo-challenge-pass-probability]]:
the SMC price-prediction alpha is dead, the obvious replacements are dead, and the value has moved
off the alpha and onto the engineering chassis + the validation discipline itself.
**Method:** six independent standalone falsification tests (each its own POC dir / tool, none touching
the bot tree) plus a module-by-module chassis audit. Every test was pre-registered with a gate and a
positive/negative control where applicable. Numbers below are the hard results, not summaries.

---

## 1. What was tested this session (six $0 falsifications)

| # | Hypothesis | Hard result | Verdict |
|---|---|---|---|
| 1 | **ML on XAUUSD M15** price features (MLP / Ridge / GBM) carries OOS edge | AUC **0.503 / 0.501 / 0.491** (≈ coin flip); net **−0.87 to −1.03 bp/trade**. **Positive control PASSED** (synthetic signal → AUC **0.693**). | DEAD — it's the **DATA, not the model**. Model class exonerated. `mlp-m15-noise-floor`. |
| 2 | **VRP** (VIX-straddle, sell rich implied vol) is a tradeable edge | Edge is **real**: IV>RV by **+4.05 vol pts** on **85.3%** of days, clears fees. But the **tail is fatal**: worst trade **−29.5%**, COVID month **−309%**, monthly maxDD **−30 to −43%** = **3–4× the FTMO 10% cap**. | REJECTED on **risk geometry** — capital-heavy + low absolute return for a retail/small-cap account. |
| 3 | **Crypto OFI** survives if re-tested on **Hyperliquid maker fees** (the old taker-fee kill might be fixable) | Net **−2.80 bp** (best case) / **−4.05 bp** (with adverse selection). Gross alpha only **0.205 bp** vs a **3 bp** maker round-trip = **~15× too thin**. Maker rebates and order-flow signals are **mechanically at odds** (you earn the rebate by being on the wrong side of flow). | DEAD — extends `crypto-ofi-poc-killed`; the maker route does not rescue it. |
| 4 | **CloudGate (LLM)** filtering adds edge over the deterministic path | **No edge**: 11 approved trades net **−$2,237**, **p = 1.000** (coin flip). Rejected arm **unmeasurable** (210/265 rejections = **API-auth infra noise**, not decisions). Structurally the LLM sees only the **same degenerate price features** → cannot add independent information. | DEAD — consistent with the 2026-06-05 LLM hot-path demotion ([[2026-06-05-llm-htf-demotion]]). |
| 5 | **FTMO $10k challenge** is worth the ~$85 / ~3,000 THB fee | Monte-Carlo (10k sims off the real net-R distribution): pass **~0% with any time limit**, **~48% coin flip with none**. Binding constraint is **trade FREQUENCY (~1 signal / 6 business days)** — too slow to compound +10% in time — **not drawdown**. | DON'T BUY — keep the 3,000 THB. Tool `tools/ftmo_challenge_mc.py` (reusable). [[2026-06-15-ftmo-challenge-pass-probability]]. |
| 6 | **Cointegration pairs trading** (stat-arb) replaces the SMC alpha on the same chassis | **Phase 1 FAIL.** 16 economically-motivated pairs (ETF + crypto): **zero** simultaneously (a) IS-cointegrated after **BH-FDR**, (b) **OOS-stable with frozen β**, (c) **tradeable half-life (≤30d)**. The two IS-survivors (SPY/IVV, EWA/EWC) both **decouple OOS**. **Gold complex NOT cointegrated 2008–2026** (miners de-rated). | DEAD at the cheap gate. Dir `E:\cointegration_poc\`. See §1a. |

### 1a. Test 6 sub-finding — a falsification almost reported off a broken instrument
The first cointegration run also said "ZERO pass," but two pairs that **must** be cointegrated
(SPY/IVV = same S&P 500 index; GLD/IAU = both spot-gold bullion) came back strongly *un*-cointegrated
(p = 0.50 / 0.99). That contradiction — not the headline — was the signal: **the test was broken, not
the pairs.** Root cause: `statsmodels.coint()`'s default `autolag='aic'` selected **29 lags** on a
persistent spread, collapsing ADF power (SPY/IVV stat −2.35, p=0.35). Fixed to a parsimonious
`maxlag=1` → SPY/IVV stat **−19.2, p=0.00000** (correctly cointegrated). The corrected, sanity-anchor-
validated test *still* failed the gate — but now for trustworthy reasons. **A surprising negative on a
sanity case means validate the instrument before you publish the conclusion.**

---

## 2. Three stale facts corrected this session (dev caught them)
Carrying wrong "known" numbers poisons every downstream decision; corrected for the record:
1. **Live P&L:** the headline **−$2,028 is 91% OLD config**. The current validated config is **n=2
   trades (−$171)** — **too few to conclude anything**. Do not cite −$2,028 as evidence against the
   current config.
2. **Walk-forward:** it is **1/3 windows pass at the relaxed t=2.5** bar (and **0/3 at the standard
   t=3.0**) — **NOT 0/3**. Quote the threshold with the result.
3. **LLM hot path:** FastGate + CloudGate were **already removed from the M5 hot path on 2026-06-05**;
   M5 is **100% deterministic**. Don't re-litigate LLM latency on the execution path — it isn't there.

---

## 3. Converged root cause (three layers, reinforced not revised)
Every dead strategy this session fails at one of three layers, in this order of lethality:
1. **Information first** — price-derived features on retail gold/crypto carry **no OOS-stable edge**
   (killed #1, #4, #6, and the SMC base in [[2026-06-13-signal-rootcause-smc-degenerate]]).
2. **Costs second** — the rare *real* micro-edges sit **below the fee floor** (#3: 0.205 bp vs 3 bp).
3. **Risk-geometry third** — a real, fee-clearing edge can still be **untradeable on a small
   capped-DD account** because of tail shape (#2 VRP: −309% month vs a 10% cap).
The **model class is exonerated** by the positive control (#1 synthetic AUC 0.693): better estimators
will not manufacture information the data does not contain.

---

## 4. The two separate probabilities (do not conflate)
- **P(this bot makes durable money) ≈ 5%.** The SMC alpha is the noise floor; no tested replacement
  cleared the bar.
- **P(the dev reaches autonomy via the skills built) ≈ 55–70%.** A **different path**, and the one
  with the value.
The asset is **the chassis (~85% reusable, audited module-by-module below) + the dev's falsification
discipline** — *not* the SMC alpha. Optimising the alpha further is low-EV; the chassis + discipline
transfer to whatever comes next.

---

## 5. Chassis audit result (carry into next session)
For any non-SMC strategy built on this codebase:

| Verdict | Modules | Why |
|---|---|---|
| **REUSE** | `core/event_bus`, `core/mt5_connector`, **`backtest/walk_forward` (crown jewel)**, `backtest/metrics`, `quant/hmm_regime`, and the account / daily-DD / total-DD / news / margin **risk guards** in `agents/risk_manager` | Venue-/account-level and strategy-agnostic. The WF harness (anchored expanding windows, per-window equity rebasing, subprocess fan-out, t-stat gate) is the highest-value reusable component. |
| **MODIFY / REPLACE** | `core/data_stream` (single-symbol → multi-asset), `risk_manager` **VETO 4 / VETO 5** (per-leg slot + duplicate-direction break for a market-neutral 2-leg book), `backtest/virtual_broker` (single-instrument dual-TP fill model), `execution/order_executor` (same-symbol dual-TP legs), lot **sizing** | All assume one instrument with an OB-anchored SL and a same-direction dual-TP geometry — SMC-specific. |
| **RETIRED** | `quant/signal_builder`, `quant/smc_engine`, `quant/indicators`, `execution/lot_calculator`, the dormant `agents/fast_gate` + `agents/cloud_gate` + `agents/macro_bias` | The SMC alpha being discarded. |

---

## Generalizable rule
**When a search has exhausted the obvious replacements, the deliverable is the engineering chassis and
the falsification discipline, not the alpha.** Run every hypothesis as a pre-registered $0 test with a
gate and a positive control; when a *sanity* case fails, validate the instrument before publishing the
verdict; and separate "does this strategy make money" from "does this work make the operator capable" —
they are different probabilities with different payoffs, and conflating them keeps you optimising a dead
alpha instead of banking the reusable engine.

---

## Process note (why this session is success, not failure)
The dev ran **~13 hope → test → close loops in a single session**, and **every negative was a clean,
documented $0 falsification** — no money spent, no live system touched, each result reproducible. That
is **elite research discipline**, not thrashing. The honest stopping point was reached **with evidence**:
retail-accessible, small-capital, automated **price/stat-arb edges are exhausted** for this data stack.
The correct next move is a human strategic call (freeze the bot as a small capped-risk component vs.
repoint the chassis at a genuinely different data/edge class), not another estimator on the same features.

---

## Artifacts (read-only)
- `E:\cointegration_poc\phase1_scan.py` + `results\phase1_results.json` — the 16-pair cointegration scan (test 6); standalone, touches no bot files.
- `tools/ftmo_challenge_mc.py` — the FTMO Monte-Carlo simulator (test 5; reusable).
- `report_costs_065.json` (IS), `report_w1..w5.json` (OOS walk-forward folds) — edge sources reused across tests.
- Memory entries written this arc: `mlp-m15-noise-floor`, `crypto-ofi-poc-killed`, `ftmo-10k-challenge-pass-prob`.

## Related
[[2026-06-15-ftmo-challenge-pass-probability]] (the fee-gate decision this generalises) ·
[[2026-06-13-signal-rootcause-smc-degenerate]] (SMC base = ~1 noisy dimension, overfits OOS) ·
[[2026-06-13-h1-limit-phase1]] (execution was the symptom; signal/feature design is the root) ·
[[2026-06-05-llm-htf-demotion]] (LLM already off the M5 hot path — corrects fact #3)

## Promotion candidate (per protocol — NOT yet promoted)
The generalizable rule (exhausted search → chassis + discipline are the asset; validate the instrument
on a sanity case before publishing a surprising negative) is a strong SKILL.md candidate — it is general
research hygiene beyond this project. Hold for one more instance per protocol, or promote if it recurs.
