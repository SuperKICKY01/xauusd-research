# Lesson 2026-05-29 — Pending Limit Order System (market → limit migration)

**Date:** 2026-05-29.
**Status:** Implemented (12-file change). Backtest gate (req 12) on
`data/xauusd_m5.csv` window 2026-02-01 → 2026-05-06:
**GATE FAILED — `gate_pass: false` (limit PF 1.599 < market PF 1.718). NOT deployed.**
Live deploy is gated on **limit PF ≥ market PF**; the limit system stays
dormant pending a re-test (see RESULTS + Decision below).
**Predecessors:**
- `2026-05-25-ob-invalidation-fix.md` — the Bug-1 OB-history *close-through-body*
  rule, reused verbatim for pending-cancellation reason 5a (OB invalidated).
- `2026-05-06-signal-spam-and-ob-reentry-spec.md` — the `_traded_obs`
  one-trade-per-OB ledger, now **locked at placement / released on unfilled cancel**
  instead of locked on fill.

## What we built

Replaced market execution with **pending limit orders** that rest at the OB
**proximal edge** and expire at the end of the next H1 candle (≤2h). Twelve
requirements, all in:

1. Pending `BUY_LIMIT`/`SELL_LIMIT` instead of market deals.
2. Limit price = OB proximal edge (top for bullish/long, bottom for bearish/short).
3. Expiration via `ORDER_TIME_SPECIFIED`, server-time, end of the Nth H1 candle.
4. Pending orders **count toward `max_concurrent`**; **one signal = one slot**
   (group by `(symbol, direction)` across positions + pending).
5. Cancellation on (a) OB BROKEN, (b) H1 BOS/CHoCH reversal, (c) expiry — plus
   news and emergency safety cancels.
6. On fill: no slippage gate (limit = no entry slippage); register to
   BreakEvenMonitor via the unchanged `TRADE_EXECUTED` payload.
7. Two-leg dual-TP with a 3-second partial-fill sync guard (native MT5 pending
   orders are single-TP, so "one order + split" is not broker-supported).
8. Side validation: `BUY_LIMIT < ask`, `SELL_LIMIT > bid`; wrong side → SKIP+log.
9. Dynamic filling-mode ladder FOK → IOC → RETURN from `symbol_info.filling_mode`.
10. Fill verification by polling `positions_get`.
11. Retcode 10004/10015 retry; **no market fallback** — SKIP+log on exhaustion.
12. Backtest limit-vs-market gate.

## Design rationale (the load-bearing decisions)

- **Proximal-edge entry is a strategy change, not just an execution change.**
  Entry moved from `ob.mid` to `ob.proximal`; SL stays anchored to `ob.distal`
  (unchanged numerically — still below/above the whole zone). This widens the
  stop (full OB height + buffer vs half), shrinks the lot, and pushes TP1/TP2
  out — RR is preserved by construction. Because expectancy changes, req 12
  re-derives the baseline (echoes 2026-05-25 Rule B: a structural change
  upstream invalidates a prior headline number).

- **One signal = one slot (group counting).** Old VETO 4 counted *legs*
  (`len(positions_get())`), so a dual-TP signal ate both slots. New VETO 4
  counts distinct `(symbol, direction)` groups across **positions + pending**.
  Net effect: ceiling rises from "2 legs" to "2 signals (≤4 legs)", but
  concurrent **risk is unchanged (~1.0%)** because each signal is still sized to
  the per-trade risk. Counting pending is what actually "prevents overlot from
  multiple pending fills" — paired with VETO 5 now also seeing pending orders.

- **Lifecycle split: OrderExecutor places, PendingOrderManager owns the rest.**
  Placement is a one-shot reaction to a `TRADE_DECISION`; the lifecycle
  (fill/cancel/expire) is a continuous poll loop watching fresh structure.
  Keeping `TRADE_EXECUTED` fired **on fill** (same payload shape) means
  BreakEvenMonitor / Notifier / TradeLogger are untouched. New events:
  `ORDER_PENDING` (locks OB) and `ORDER_CANCELLED` (releases OB on no-fill).

- **OB lock moved from fill-time to placement-time.** With a placement→fill gap,
  the same OB could spawn a second pending order. So lock on `ORDER_PENDING`,
  release on `ORDER_CANCELLED` *only when no leg filled* (so a still-valid OB
  re-arms on a later retrace), keep locked once any leg fills.

- **Cancellation reuses existing math.** 5a applies the Bug-1 close-through-body
  rule to fresh **M15** closes after placement (the OB lives on M15). 5b runs
  `find_structure_events` on fresh **H1** and cancels on an opposite BOS/CHoCH
  *after* placement — **H1 only, never M5 noise** (explicit requirement).

- **Safety: pending orders must also be cancelled on news & emergency.** A
  resting limit filling into an NFP spike is exactly the VETO-1 failure mode,
  and a limit filling into a margin/DD breach defeats the emergency liquidator.
  News cancel lives in the manager; emergency cancel lives in `main.py`
  (`_close_all_positions`) because the manager task is torn down during shutdown.

- **2-leg + 3s sync guard (req 7).** Native MT5 pending orders carry a single
  TP, so "one order + split" is not broker-supported → we use two limit legs at
  the same price; if one fills and the sibling doesn't within 3s, cancel the
  sibling and run SOLO. Keeps lot/slot accounting clean.

- **Backtest gate is conservative by construction.** The limit model fills at
  exactly the limit (no slippage) when a bar's range touches it, else counts a
  miss at expiry. OB-broken / H1-reversal cancellation is **not** modelled in
  the backtest — those only remove dead-thesis trades *before* fill, so omitting
  them makes the limit PF a **lower bound**. Limit fills are price-deterministic
  (unlike the latency-sensitive market slippage gate, which the backtest still
  cannot model — see 2026-05-25 Sub-lesson 2), so the backtest is a *more*
  faithful predictor for limit orders than for market orders.

## Backtest gate (req 12) — RESULTS

Source: `report_limit_vs_market.json` (run 2026-05-29 23:38),
`data/xauusd_m5.csv` window 2026-02-01 → 2026-05-06, risk 0.5%.

| Metric | market (current: mid+market) | limit (new: proximal+pending) |
|---|---:|---:|
| Profit factor | 1.718 | 1.599 |
| Trades (legs) | 58 | 32 |
| Win rate % | 32.76 | 37.50 |
| Net PnL | $5,323.31 | $3,232.09 |
| Max DD % | 1.914 | 1.669 |
| Limit fill rate | — | 48.48% (16/33) |
| Limit missed | — | 17 |

**Gate (limit PF ≥ market PF): FAIL** — `gate_pass: false`
(1.599 < 1.718, short by 0.119). The limit set wins on per-trade quality
(higher win rate, expectancy $101 vs $91.78, RR 0.391 vs 0.363, lower DD)
but the 48.5% fill rate guts realized volume → net PnL ~$2.1k below market.
Sample is small (16 fills, t-stat 1.598) and the underlying strategy has
**not** cleared walk-forward validation (`wfv_report.json`: `all_passed: false`).

## Decision (2026-05-30)

**Do NOT deploy limit orders to live.** Keep the limit infrastructure in
place (all 12 files), dormant, for a future re-test once the fill rate is
improved and WFV passes. Live execution is to revert to the market baseline
(entry at OB mid, `ENTRY_AT_PROXIMAL = False`).

**Resolution (rebuild behind a switch).** The entry-geometry revert in
`signal_builder.py` is one line, but it does **not** by itself restore market
execution — the execution half of this migration had made
`execution/order_executor.py` **limit-only** (old market-order opening path
replaced, no git to revert). So a market path was reconstructed and gated:

- **`config.py`** — new `ExecutionConfig` with `mode` (`market` | `limit`,
  default **`market`**, env `EXECUTION_MODE`), `max_slippage_frac` (0.25),
  `market_deviation_points` (30).
- **`quant/signal_builder.py`** — `ENTRY_AT_PROXIMAL = False` (entry at OB mid),
  coupled-by-comment to `CFG.execution.mode`.
- **`execution/order_executor.py`** — `start()` dispatches on
  `CFG.execution.mode`; new `_handle_market_decision` / `_send_market_with_retry`
  / `_resolve_market_filling_modes` send `TRADE_ACTION_DEAL` market deals with the
  reconstructed slippage gate (`Excessive slippage … planned_entry= live=
  planned_SL_distance=` — matches `tools/replay_slippage_rejects.py` regex), and
  publish the **same** `TRADE_EXECUTED` / `POSITION_OPENED` payloads as
  `PendingOrderManager._finalize_fill`. All limit code is untouched and dormant.
- **`main.py`** — unchanged; `pending_order_manager` idles harmlessly in market
  mode (no `ORDER_PENDING` events, no resting orders to recover).

Verified: `py_compile` clean; all referenced `mt5.*` constants present
(`hasattr` probe, Rule B); `CFG.execution.mode='market'`, `ENTRY_AT_PROXIMAL=False`.
**NOT yet demo-verified** — the market path is reconstructed-from-spec code and
must be validated against a demo account (fill ticket vs `result.order`, slippage
rejections logging to the `rejections` table, dual-TP + BE) before any live use.

⚠️ **Concurrency parity gap (open risk item).** The limit migration changed
live VETO 4 (`risk_manager.py`) to **group-counting** (≤2 `(symbol,direction)`
groups = ≤2 signals = up to 4 legs), but `backtest/virtual_broker.py` VETO 4
still counts **legs** (≤2 legs = ≤1 dual-TP signal at a time). So the market
baseline (PF 1.718, DD 1.914%) was measured at one-signal-at-a-time, while live
market mode would allow two concurrent signals → ~2× concurrent exposure; the
backtested DD/PF may not hold live. Not resolved here (it's a live risk-param
decision). Cleanest alignment: run live with `MAX_CONCURRENT_TRADES=1` (group
cap → 1 signal, matching the backtest), OR migrate VirtualBroker VETO 4 to
group-counting and re-baseline. Decide before live.

⚠️ Flip rule: to re-test limit later, set `EXECUTION_MODE=limit` **and**
`ENTRY_AT_PROXIMAL = True` together — one without the other is an untested hybrid.

## Generalizable rules

### Rule A — A resting order is a live risk; every kill-switch must also cancel pending

News blocks, emergency liquidation, and concurrency caps were all written for
*positions*. The moment you introduce resting orders, each of those guards has a
blind spot: an order that fills *after* the guard fired. Audit every
risk gate for "does this also handle orders that haven't filled yet?" when
adding any deferred-execution mechanism.

### Rule B — Verify external-library constants exist; don't trust the docs

`SYMBOL_FILLING_FOK` / `SYMBOL_FILLING_IOC` are documented MT5 enum constants but
are **not exposed** by the installed `MetaTrader5` Python package. `py_compile`
and even a plain import won't catch it — the `AttributeError` only fires when the
code path runs. A one-line `hasattr` sweep over every new `mt5.*` constant caught
it before live. **Trigger:** any time you reference a new constant/attribute from
a third-party C-extension wrapper, probe it with `hasattr` before shipping.

### Rule C — Identity locks for deferred actions need an explicit release path

The `_traded_obs` ledger was append-only (lock on fill, never release) because
market fills were instantaneous — "placed" and "filled" were the same moment.
Deferring the fill split that into two events and exposed the missing release
semantics: lock on intent (placement), release on abandonment (unfilled cancel),
keep on commitment (fill). Any "mark as used" ledger guarding a *deferred* action
needs all three transitions, not just the lock.

## Promotion candidates

- **Rule A → SKILL.md anti-patterns.** Strong candidate (#8): *"A resting/pending
  order is a live risk — every kill-switch (news, emergency, DD, concurrency)
  must also cancel pending orders, not just positions."* Recommend promoting now;
  it is a permanent invariant of the limit-order architecture.
- **Rule B → SKILL.md anti-patterns.** Candidate: *"Probe new `mt5.*` constants
  with `hasattr` before shipping — the Python package does not expose every
  documented enum."* Wait for one more instance before promoting (avoid premature
  promotion), per protocol.
- **Rule C → not promoted.** Specific to identity-ledger design; stays as audit
  trail unless it recurs.

## Out-of-scope / deferred

1. **Backtest cancellation modelling.** OB-broken / H1-reversal cancels are live-only;
   the backtest omits them (conservative). Modelling them would require threading
   M15/H1 structure into the per-bar broker step — a larger change, only worth it
   if the conservative gate is marginal.
2. **Recovered-order OB identity.** Pending orders recovered across a restart have
   no reconstructable `ob_identity`, so OB-broken cancel + OB-lock release don't
   apply to them (H1-reversal + expiry still guard them). Acceptable for the rare
   restart-with-pending case.
3. **Single-order + managed partial close (req 7 alternative).** Would eliminate
   the partial-fill race entirely and make "one signal = one slot" atomic, at the
   cost of a larger BE/TP1 rewrite. Deferred; the 2-leg + 3s guard was chosen.
