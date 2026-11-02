# Portfolio TSMOM — Phase 2 hardening: reviewer handoff

**Date:** 2026-06-20 · **$0**, free data (yfinance + cached Binance) · **seed=42** (bootstrap only) ·
**bot 6168 untouched.** Reproduce: `tsmom_portfolio_phase2.py` → `phase2_run_log.txt`.
Universe: 41 instruments (37 ETF/FX + 4 crypto), 1998–2026, headline cfg = 12-month TSMOM, tanh,
per-asset inverse-vol, monthly rebalance, 12% portfolio vol-target. Frictions (documented): 5bp/8bp
trade spread, 1%/yr short-borrow; financing shown separately (futures≈0 vs ETF-margin 2.5%).

## Phase-1 headline (for context)
annSR 0.89; CPCV mean OOS 0.86 (100% folds>0); DSR 0.982 @N=86; spanning α t=4.5, equity-β 0.05;
no look-ahead (survives +1/+4d lag, reverses to −0.92, raw-trend Sharpe 0.64).

## The 6 hardening tests — number, and what survived / died

| # | Test | Result | Survives? |
|---|---|---|---|
| 1 | **Ex-crypto** | full SR 0.82→**0.80** ex-crypto; DSR 0.962→0.949. Crypto adds ~nothing. | **✓ not a crypto artifact** |
| 2 | **Real frictions** | gross 0.90 → spread 0.87 → +borrow1% 0.82 → +borrow2% 0.78 → **+financing2.5% 0.54**. Trade/borrow cheap; **financing is the killer, but only in the ETF/retail-leverage case** (futures≈0). | **✓ as futures / ✗ as levered ETF** |
| 3 | **Modern era** | 2015-26: SR 0.79, α t=2.17 (p=0.030) **but DSR 0.581**; ex-crypto 2015-26 α t=1.95 (**p=0.051**), **DSR 0.494**. 2010-26 DSR 0.69. | **✗ modern DSR fails — full-sample pass leaned on pre-2010** |
| 4 | **Effective breadth** | N_eff≈4 (avg corr +0.23). Dependence-aware: block-bootstrap SR 90%CI full [0.51,1.13] / modern [0.30,1.24], P(SR>0)=100%; HAC t(mean) 4.44 full / 2.77 modern. (DSR's N=trials=86, never the asset count.) | **✓ return series robustly >0 under dependence** |
| 5 | **Multi-factor vs DBMF** (KEY) | corr to DBMF +0.56. Regress on SPY/TLT/DBC/**DBMF** (2019-26): **β(DBMF)=0.66 (t=15.7)**, β(TLT)=0.22 (t=8.1), β(SPY)=0.13; **α=+7.1%/yr, t=1.82, p=0.068** (ex-crypto α +5.6%, **p=0.156**). | **✗ no significant α beyond the buyable trend ETF** |
| 6 | **Capital adequacy / subset** | Share-granularity not binding (37/41 legs at $10k). **Binding constraint = 2.5x gross leverage** (p95 5.3x) > Reg-T 2x → needs futures/PM. Breadth→edge: K=6 DSR 0.73 → K=15 0.92 → K=41 0.96 (cutting breadth re-creates the Test-17 collapse). Executable retail book (12 liquid ETFs+BTC, ≤2x): SR 0.63, ~8%/yr. | **partial — executable only as a reduced ≤2x book = a worse DBMF** |

## Single honest verdict
**There is a REAL trend premium here, but it is the already-harvested, publicly-buyable
time-series-momentum / managed-futures premium — NOT proprietary alpha.**
- It is not a crypto artifact, not a look-ahead bug, and the return series is robustly positive under
  dependence-aware testing (HAC t 2.77–4.44, bootstrap CI excludes 0). That part is real.
- **But it fails the pre-registered Phase-2 gate on two decisive counts:**
  1. **No statistically significant α beyond DBMF** (p=0.068 with crypto, p=0.156 ex-crypto; β to DBMF
     0.66). After our own frictions the beyond-DBMF α is ~nil. We rediscovered a factor you can buy for
     **0.85%/yr in one ticker.**
  2. **Modern-era (post-2015) DSR does not clear** (0.49–0.58) — the full-sample PASS is carried by the
     strong pre-2010 era, consistent with the documented post-2010 TSMOM crowding/decay.
- **Executable?** The full-strength book needs ~2.5x gross leverage, infeasible at retail (Reg-T 2x).
  The actually-executable ≤2x retail book (12 liquid ETFs + BTC) earns **~0.63 Sharpe, ~8%/yr at 13.4%
  vol** — i.e. a strictly *worse*, less-diversified, self-managed DBMF.

**Does building it beat buying DBMF? No.** Same exposure, professionally executed and more diversified,
for 0.85%/yr in one ticker.

## Realistic net return profile of the EXECUTABLE book (12 liquid ETFs + BTC, ≤2x, post-frictions)
| era | annSR | vol | CAGR | $/yr @ $10k | @ $50k | @ $100k |
|---|---|---|---|---|---|---|
| full 1998-2026 | 0.63 | 13.4% | +8.2%/yr | +$817 | +$4,083 | +$8,166 |
| modern 2015-26 | 0.66 | 13.5% | +8.6%/yr | +$859 | +$4,295 | +$8,589 |

**Minimum capital where it's executable & economically meaningful:** mechanically holdable from ~$10k,
but only as the reduced ≤2x book (the full 41-leg, 2.5x book needs ~6-figure capital + futures/portfolio
margin). At every capital level the economically rational choice is **buy DBMF**, not replicate it.
And it remains **orthogonal to the project goal** (single-symbol, short-horizon, tight-DD FTMO XAUUSD).

## Recommendation
Do not build/deploy. The Phase-1 PASS was genuine but it is the public trend factor; there is no
modern, post-cost, beyond-DBMF alpha to justify the engineering. STOP for review.
